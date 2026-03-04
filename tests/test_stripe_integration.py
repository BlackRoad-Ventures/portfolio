"""
Tests for BlackRoad Ventures Stripe Integration.
Unit tests for PaymentLedger, WebhookProcessor, PaymentService.
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from stripe_integration import (
    Payment,
    PaymentLedger,
    PaymentService,
    PaymentStatus,
    PayoutRoute,
    PlanTier,
    PLAN_PRICES,
    StripeClient,
    Subscription,
    SubscriptionStatus,
    WebhookEvent,
    WebhookProcessor,
)


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_ledger_db(tmp_path):
    return tmp_path / "stripe_ledger.db"


@pytest.fixture
def ledger(tmp_ledger_db):
    lg = PaymentLedger(tmp_ledger_db)
    yield lg
    lg.close()


@pytest.fixture
def service(tmp_ledger_db):
    svc = PaymentService(stripe_client=None, db_path=tmp_ledger_db)
    yield svc
    svc.close()


# ─── Payment dataclass tests ────────────────────────────────────────────────


class TestPayment:
    def test_amount_dollars(self):
        p = Payment(id="p1", customer_id="c1", amount_cents=9900)
        assert p.amount_dollars == 99.0

    def test_is_complete_false(self):
        p = Payment(id="p1", customer_id="c1", amount_cents=100)
        assert not p.is_complete

    def test_is_complete_true(self):
        p = Payment(id="p1", customer_id="c1", amount_cents=100,
                    status=PaymentStatus.SUCCEEDED)
        assert p.is_complete

    def test_default_currency(self):
        p = Payment(id="p1", customer_id="c1", amount_cents=100)
        assert p.currency == "usd"


# ─── Plan pricing tests ─────────────────────────────────────────────────────


class TestPlanPricing:
    def test_starter_monthly(self):
        assert PLAN_PRICES[PlanTier.STARTER]["monthly"] == 2900

    def test_growth_yearly(self):
        assert PLAN_PRICES[PlanTier.GROWTH]["yearly"] == 99000

    def test_enterprise_monthly(self):
        assert PLAN_PRICES[PlanTier.ENTERPRISE]["monthly"] == 49900

    def test_yearly_discount(self):
        for tier in PlanTier:
            monthly_annual = PLAN_PRICES[tier]["monthly"] * 12
            yearly = PLAN_PRICES[tier]["yearly"]
            assert yearly <= monthly_annual


# ─── PaymentLedger tests ────────────────────────────────────────────────────


class TestPaymentLedger:
    def test_record_and_get_payment(self, ledger):
        p = Payment(id="pay_1", customer_id="cus_1", amount_cents=5000)
        ledger.record_payment(p)
        loaded = ledger.get_payment("pay_1")
        assert loaded is not None
        assert loaded.customer_id == "cus_1"
        assert loaded.amount_cents == 5000

    def test_get_nonexistent_payment(self, ledger):
        assert ledger.get_payment("nonexistent") is None

    def test_update_payment_status(self, ledger):
        p = Payment(id="pay_2", customer_id="cus_1", amount_cents=1000)
        ledger.record_payment(p)
        ledger.update_payment_status("pay_2", PaymentStatus.SUCCEEDED, "pi_stripe_123")
        loaded = ledger.get_payment("pay_2")
        assert loaded.status == PaymentStatus.SUCCEEDED
        assert loaded.stripe_payment_id == "pi_stripe_123"

    def test_customer_payments(self, ledger):
        for i in range(3):
            ledger.record_payment(
                Payment(id=f"pay_{i}", customer_id="cus_A", amount_cents=100 * (i + 1))
            )
        ledger.record_payment(
            Payment(id="pay_other", customer_id="cus_B", amount_cents=500)
        )
        payments = ledger.get_customer_payments("cus_A")
        assert len(payments) == 3
        assert all(p.customer_id == "cus_A" for p in payments)

    def test_idempotency_check(self, ledger):
        p = Payment(
            id="pay_idem", customer_id="cus_1", amount_cents=9900,
            idempotency_key="key_unique_123",
        )
        ledger.record_payment(p)
        found = ledger.check_idempotency("key_unique_123")
        assert found is not None
        assert found.id == "pay_idem"

    def test_idempotency_key_not_found(self, ledger):
        assert ledger.check_idempotency("no_such_key") is None

    def test_duplicate_payment_ignored(self, ledger):
        p = Payment(id="pay_dup", customer_id="cus_1", amount_cents=100)
        ledger.record_payment(p)
        ledger.record_payment(p)  # Should not raise
        loaded = ledger.get_payment("pay_dup")
        assert loaded is not None


class TestSubscriptionLedger:
    def test_record_and_get_subscription(self, ledger):
        sub = Subscription(id="sub_1", customer_id="cus_1", plan_id="starter")
        ledger.record_subscription(sub)
        loaded = ledger.get_subscription("sub_1")
        assert loaded is not None
        assert loaded.plan_id == "starter"
        assert loaded.status == "active"

    def test_update_subscription_status(self, ledger):
        sub = Subscription(id="sub_2", customer_id="cus_1", plan_id="growth")
        ledger.record_subscription(sub)
        ledger.update_subscription_status("sub_2", SubscriptionStatus.CANCELLED)
        loaded = ledger.get_subscription("sub_2")
        assert loaded.status == SubscriptionStatus.CANCELLED

    def test_active_subscriptions(self, ledger):
        ledger.record_subscription(
            Subscription(id="sub_a1", customer_id="cus_1", plan_id="starter")
        )
        ledger.record_subscription(
            Subscription(id="sub_a2", customer_id="cus_1", plan_id="growth")
        )
        ledger.record_subscription(
            Subscription(id="sub_a3", customer_id="cus_1", plan_id="enterprise",
                         status=SubscriptionStatus.CANCELLED)
        )
        active = ledger.get_active_subscriptions("cus_1")
        assert len(active) == 2

    def test_get_nonexistent_subscription(self, ledger):
        assert ledger.get_subscription("no_sub") is None


# ─── Webhook event tests ────────────────────────────────────────────────────


class TestWebhookEvents:
    def test_record_new_event(self, ledger):
        event = WebhookEvent(
            stripe_event_id="evt_1",
            event_type="payment_intent.succeeded",
            payload={"id": "evt_1", "type": "payment_intent.succeeded"},
        )
        is_new = ledger.record_webhook_event(event)
        assert is_new is True

    def test_duplicate_event_rejected(self, ledger):
        event = WebhookEvent(
            stripe_event_id="evt_dup",
            event_type="payment_intent.succeeded",
            payload={"id": "evt_dup"},
        )
        assert ledger.record_webhook_event(event) is True
        assert ledger.record_webhook_event(event) is False

    def test_mark_event_processed(self, ledger):
        event = WebhookEvent(
            stripe_event_id="evt_proc",
            event_type="test.event",
            payload={},
        )
        ledger.record_webhook_event(event)
        ledger.mark_event_processed("evt_proc")
        unprocessed = ledger.get_unprocessed_events()
        assert all(e.stripe_event_id != "evt_proc" for e in unprocessed)

    def test_get_unprocessed_events(self, ledger):
        for i in range(3):
            ledger.record_webhook_event(WebhookEvent(
                stripe_event_id=f"evt_{i}",
                event_type="test.event",
                payload={},
            ))
        ledger.mark_event_processed("evt_1")
        unprocessed = ledger.get_unprocessed_events()
        assert len(unprocessed) == 2


# ─── Payout route tests ─────────────────────────────────────────────────────


class TestPayoutRoutes:
    def test_add_payout_route(self, ledger):
        route = PayoutRoute(
            destination="pi-node-01.local",
            destination_type="pi",
            percentage=60.0,
        )
        row_id = ledger.add_payout_route(route)
        assert row_id > 0

    def test_get_active_routes(self, ledger):
        ledger.add_payout_route(PayoutRoute("pi-01.local", "pi", percentage=50.0))
        ledger.add_payout_route(PayoutRoute("pi-02.local", "pi", percentage=50.0))
        routes = ledger.get_active_routes()
        assert len(routes) == 2
        assert sum(r.percentage for r in routes) == 100.0

    def test_route_with_connected_account(self, ledger):
        ledger.add_payout_route(PayoutRoute(
            destination="pi-main",
            destination_type="pi",
            connected_account="acct_pi_001",
            percentage=100.0,
        ))
        routes = ledger.get_active_routes()
        assert routes[0].connected_account == "acct_pi_001"


# ─── WebhookProcessor tests ─────────────────────────────────────────────────


class TestWebhookProcessor:
    def test_register_and_dispatch(self, ledger):
        processor = WebhookProcessor(ledger)
        handled = []
        processor.register("test.event", lambda e: handled.append(e))

        event = WebhookEvent(
            stripe_event_id="evt_dispatch",
            event_type="test.event",
            payload={"data": "test"},
        )
        result = processor.process_event(event)
        assert result is True
        assert len(handled) == 1

    def test_duplicate_event_not_reprocessed(self, ledger):
        processor = WebhookProcessor(ledger)
        call_count = [0]
        processor.register("test.event", lambda e: call_count.__setitem__(0, call_count[0] + 1))

        event = WebhookEvent(
            stripe_event_id="evt_once",
            event_type="test.event",
            payload={},
        )
        processor.process_event(event)
        processor.process_event(event)
        assert call_count[0] == 1

    def test_unhandled_event_type(self, ledger):
        processor = WebhookProcessor(ledger)
        event = WebhookEvent(
            stripe_event_id="evt_unknown",
            event_type="unknown.event",
            payload={},
        )
        result = processor.process_event(event)
        assert result is True  # Still recorded, just no handler


# ─── PaymentService tests ───────────────────────────────────────────────────


class TestPaymentService:
    def test_create_payment_offline(self, service):
        payment = service.create_payment(
            payment_id="svc_pay_1",
            customer_id="cus_1",
            amount_cents=5000,
            description="Test payment",
        )
        assert payment.id == "svc_pay_1"
        assert payment.amount_cents == 5000

    def test_create_payment_idempotent(self, service):
        p1 = service.create_payment(
            payment_id="svc_idem_1",
            customer_id="cus_1",
            amount_cents=5000,
            idempotency_key="idem_key_abc",
        )
        p2 = service.create_payment(
            payment_id="svc_idem_2",
            customer_id="cus_1",
            amount_cents=5000,
            idempotency_key="idem_key_abc",
        )
        assert p1.id == p2.id

    def test_get_payment(self, service):
        service.create_payment("svc_get", "cus_1", 1000)
        loaded = service.get_payment("svc_get")
        assert loaded is not None
        assert loaded.customer_id == "cus_1"

    def test_create_subscription(self, service):
        sub = service.create_subscription(
            sub_id="svc_sub_1",
            customer_id="cus_1",
            plan_id="growth",
        )
        assert sub.status == "active"
        assert sub.plan_id == "growth"

    def test_cancel_subscription(self, service):
        service.create_subscription("svc_sub_cancel", "cus_1", "starter")
        result = service.cancel_subscription("svc_sub_cancel")
        assert result is True

    def test_cancel_nonexistent_subscription(self, service):
        assert service.cancel_subscription("nope") is False

    def test_active_subscriptions(self, service):
        service.create_subscription("sub_act_1", "cus_1", "starter")
        service.create_subscription("sub_act_2", "cus_1", "growth")
        active = service.get_active_subscriptions("cus_1")
        assert len(active) == 2

    def test_add_and_get_payout_routes(self, service):
        service.add_payout_route("pi-node-1.local", "pi", percentage=60.0)
        service.add_payout_route("pi-node-2.local", "pi", percentage=40.0)
        routes = service.get_active_routes()
        assert len(routes) == 2

    def test_route_payout_offline(self, service):
        service.add_payout_route("pi-01", "pi", percentage=70.0)
        service.add_payout_route("pi-02", "pi", percentage=30.0)
        results = service.route_payout(10000, "test payout")
        assert len(results) == 2
        assert results[0]["amount_cents"] == 7000
        assert results[1]["amount_cents"] == 3000
        assert all(r["status"] == "routed" for r in results)

    def test_route_payout_no_routes(self, service):
        results = service.route_payout(5000)
        assert results == []

    def test_handle_webhook_invalid_json(self, service):
        ok, msg = service.handle_webhook(b"not json", "", "")
        assert ok is False
        assert "Invalid JSON" in msg

    def test_handle_webhook_missing_id(self, service):
        payload = json.dumps({"type": "test"}).encode()
        ok, msg = service.handle_webhook(payload, "", "")
        assert ok is False

    def test_handle_webhook_valid(self, service):
        payload = json.dumps({
            "id": "evt_webhook_test",
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": "pi_123"}},
        }).encode()
        ok, msg = service.handle_webhook(payload, "", "")
        assert ok is True
        assert "payment_intent.succeeded" in msg

    def test_handle_webhook_dedup(self, service):
        payload = json.dumps({
            "id": "evt_dedup_test",
            "type": "test.event",
            "data": {},
        }).encode()
        ok1, _ = service.handle_webhook(payload, "", "")
        ok2, msg2 = service.handle_webhook(payload, "", "")
        assert ok1 is True
        assert ok2 is True
        assert "Already processed" in msg2


# ─── Webhook signature verification ─────────────────────────────────────────


class TestWebhookSignatureVerification:
    def test_valid_signature(self):
        secret = "whsec_test_secret"
        payload = b'{"id":"evt_1","type":"test"}'
        timestamp = str(int(time.time()))
        signed = f"{timestamp}.".encode() + payload
        import hashlib
        import hmac as _hmac
        sig = _hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        header = f"t={timestamp},v1={sig}"

        assert StripeClient.verify_webhook_signature(payload, header, secret) is True

    def test_invalid_signature(self):
        assert StripeClient.verify_webhook_signature(
            b"payload", "t=123,v1=badsig", "secret"
        ) is False

    def test_expired_timestamp(self):
        secret = "whsec_test"
        payload = b'{"test": true}'
        old_ts = str(int(time.time()) - 600)
        signed = f"{old_ts}.".encode() + payload
        import hashlib
        import hmac as _hmac
        sig = _hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        header = f"t={old_ts},v1={sig}"

        assert StripeClient.verify_webhook_signature(payload, header, secret) is False

    def test_malformed_header(self):
        assert StripeClient.verify_webhook_signature(
            b"payload", "garbage", "secret"
        ) is False


# ─── Webhook handler integration tests ──────────────────────────────────────


class TestWebhookHandlers:
    def test_payment_succeeded_updates_ledger(self, service):
        service.create_payment("pay_wh_1", "cus_1", 5000)
        service.ledger.update_payment_status("pay_wh_1", "processing", "pi_stripe_wh")

        payload = json.dumps({
            "id": "evt_pay_success",
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": "pi_stripe_wh"}},
        }).encode()
        service.handle_webhook(payload, "", "")

        loaded = service.get_payment("pay_wh_1")
        assert loaded.status == PaymentStatus.SUCCEEDED

    def test_payment_failed_updates_ledger(self, service):
        service.create_payment("pay_wh_fail", "cus_1", 3000)
        service.ledger.update_payment_status("pay_wh_fail", "processing", "pi_fail_wh")

        payload = json.dumps({
            "id": "evt_pay_fail",
            "type": "payment_intent.payment_failed",
            "data": {"object": {"id": "pi_fail_wh"}},
        }).encode()
        service.handle_webhook(payload, "", "")

        loaded = service.get_payment("pay_wh_fail")
        assert loaded.status == PaymentStatus.FAILED

    def test_subscription_created_via_webhook(self, service):
        payload = json.dumps({
            "id": "evt_sub_create",
            "type": "customer.subscription.created",
            "data": {"object": {
                "id": "sub_stripe_new",
                "customer": "cus_webhook",
                "plan": {"id": "price_growth"},
            }},
        }).encode()
        service.handle_webhook(payload, "", "")

        loaded = service.ledger.get_subscription("sub_stripe_new")
        assert loaded is not None
        assert loaded.customer_id == "cus_webhook"

    def test_subscription_cancelled_via_webhook(self, service):
        service.create_subscription("sub_wh_cancel", "cus_1", "starter")
        service.ledger.conn.execute(
            "UPDATE subscriptions SET stripe_sub_id='sub_wh_cancel' WHERE id='sub_wh_cancel'"
        )
        service.ledger.conn.commit()

        payload = json.dumps({
            "id": "evt_sub_cancel",
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": "sub_wh_cancel"}},
        }).encode()
        service.handle_webhook(payload, "", "")

        loaded = service.ledger.get_subscription("sub_wh_cancel")
        assert loaded.status == SubscriptionStatus.CANCELLED
