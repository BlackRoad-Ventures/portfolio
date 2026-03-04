"""
BlackRoad Ventures — End-to-End Integration Tests
===================================================
Full E2E flows testing the complete application pipeline:
Portfolio creation -> Stripe payments -> Subscription management ->
Payout routing -> Cap table updates -> Analytics.

These tests validate the entire system working together with
real SQLite databases (temp) but mocked Stripe API calls.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cap_table import CapTableManager, Shareholder
from pipeline import add_deal, init_db as init_pipeline_db, move_stage, pipeline_summary
from portfolio_tracker import Asset, Portfolio, PortfolioAnalytics, PortfolioManager
from stripe_integration import (
    Payment,
    PaymentLedger,
    PaymentService,
    PaymentStatus,
    PayoutRoute,
    PlanTier,
    PLAN_PRICES,
    Subscription,
    SubscriptionStatus,
    WebhookEvent,
)


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_dbs(tmp_path):
    """Provide paths for all databases."""
    return {
        "portfolio": tmp_path / "portfolio.db",
        "captable": tmp_path / "captable.db",
        "stripe": tmp_path / "stripe.db",
        "pipeline": tmp_path / "pipeline.db",
    }


@pytest.fixture
def portfolio_mgr(tmp_dbs):
    mgr = PortfolioManager(tmp_dbs["portfolio"])
    yield mgr
    mgr.close()


@pytest.fixture
def captable_mgr(tmp_dbs):
    mgr = CapTableManager(tmp_dbs["captable"])
    yield mgr
    mgr.close()


@pytest.fixture
def payment_svc(tmp_dbs):
    svc = PaymentService(stripe_client=None, db_path=tmp_dbs["stripe"])
    yield svc
    svc.close()


# ─── E2E: Full investor onboarding flow ─────────────────────────────────────


class TestE2EInvestorOnboarding:
    """
    Complete flow: Investor signs up -> Creates subscription -> Makes payment
    -> Gets added to cap table -> Portfolio updated -> Payout routed to Pi.
    """

    def test_full_investor_lifecycle(self, portfolio_mgr, captable_mgr, payment_svc):
        # Step 1: Create portfolio and cap table
        portfolio_mgr.create("fund-1", "BlackRoad Ventures Fund I", "alexa@blackroad.io")
        captable_mgr.create("br-captable", "BlackRoad OS, Inc.")

        # Step 2: Investor subscribes to Growth plan
        sub = payment_svc.create_subscription(
            sub_id="sub_investor_001",
            customer_id="cus_investor_alice",
            plan_id=PlanTier.GROWTH,
            metadata={"investor_name": "Alice Ventures"},
        )
        assert sub.status == "active"
        assert sub.plan_id == PlanTier.GROWTH

        # Step 3: Investor makes initial investment payment
        payment = payment_svc.create_payment(
            payment_id="pay_invest_001",
            customer_id="cus_investor_alice",
            amount_cents=100_000_00,  # $100,000
            description="Series A investment — Alice Ventures",
            idempotency_key="invest_alice_series_a",
        )
        assert payment.amount_dollars == 100_000.0

        # Step 4: Simulate payment succeeding via webhook
        webhook_payload = json.dumps({
            "id": "evt_invest_success",
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": "pi_invest_alice"}},
        }).encode()

        # First update the payment with stripe ID
        payment_svc.ledger.update_payment_status(
            "pay_invest_001", PaymentStatus.PROCESSING, "pi_invest_alice"
        )
        ok, msg = payment_svc.handle_webhook(webhook_payload, "", "")
        assert ok is True

        # Verify payment completed
        completed = payment_svc.get_payment("pay_invest_001")
        assert completed.status == PaymentStatus.SUCCEEDED

        # Step 5: Add investor to cap table
        sh_id = captable_mgr.add_shareholder(
            "br-captable",
            Shareholder(
                "Alice Ventures", "institution",
                500_000, "preferred_a", 2.00,
            ),
        )
        assert sh_id > 0

        # Step 6: Add investment to portfolio
        portfolio_mgr.add_asset(
            "fund-1",
            Asset("BRDOS", 500_000, 0.20, 0.28, "fund", "venture"),
        )
        portfolio_mgr.update_prices("fund-1", {"BRDOS": 0.28})

        # Step 7: Verify portfolio reflects the investment
        summary = portfolio_mgr.portfolio_summary("fund-1")
        assert summary["asset_count"] == 1
        assert summary["total_value"] == 140_000.0  # 500k * 0.28
        assert summary["pnl"] == 40_000.0  # 500k * (0.28 - 0.20)

        # Step 8: Setup payout routing to Pi nodes
        payment_svc.add_payout_route("pi-node-01.blackroad.local", "pi", percentage=60.0)
        payment_svc.add_payout_route("pi-node-02.blackroad.local", "pi", percentage=40.0)

        # Step 9: Route profits to Pi infrastructure
        payout_results = payment_svc.route_payout(
            40_000_00,  # $40,000 in cents
            "Q1 profit distribution",
        )
        assert len(payout_results) == 2
        assert payout_results[0]["amount_cents"] == 24_000_00  # 60%
        assert payout_results[1]["amount_cents"] == 16_000_00  # 40%

        # Step 10: Verify cap table ownership
        ownership = captable_mgr.get_ownership_pct("br-captable")
        assert len(ownership) == 1
        assert ownership[0]["name"] == "Alice Ventures"
        assert ownership[0]["ownership_pct"] == 100.0  # Only shareholder


class TestE2EMultiInvestorFund:
    """
    Multiple investors, multiple payments, cap table dilution,
    portfolio rebalancing, and payout distribution.
    """

    def test_multi_investor_fund_flow(self, portfolio_mgr, captable_mgr, payment_svc):
        # Create fund and cap table
        portfolio_mgr.create("fund-2", "BlackRoad Growth Fund", "alexa@blackroad.io")
        captable_mgr.create("growth-ct", "BlackRoad Growth Co")

        # Founders
        captable_mgr.add_shareholder(
            "growth-ct",
            Shareholder("Alexa (Founder)", "founder", 4_000_000, "common", 0.001),
        )

        # Investor 1: Seed round
        payment_svc.create_payment("pay_seed_1", "cus_seed_vc", 500_000_00, description="Seed investment")
        captable_mgr.add_shareholder(
            "growth-ct",
            Shareholder("Seed VC", "institution", 1_000_000, "preferred_a", 0.50),
        )

        # Investor 2: Series A
        payment_svc.create_payment("pay_series_a", "cus_series_a", 2_000_000_00, description="Series A")
        captable_mgr.add_shareholder(
            "growth-ct",
            Shareholder("Growth Partners", "institution", 2_000_000, "preferred_b", 2.00),
        )

        # Portfolio assets
        assets = [
            Asset("BTC", 1.0, 45_000, 68_000, "crypto", "crypto"),
            Asset("ETH", 10.0, 2_500, 3_800, "crypto", "crypto"),
            Asset("NVDA", 20, 500, 900, "equity", "technology"),
            Asset("SPY", 10, 420, 520, "etf", "diversified"),
        ]
        for a in assets:
            portfolio_mgr.add_asset("fund-2", a)
            portfolio_mgr.update_prices("fund-2", {a.ticker: a.current_price})

        # Verify portfolio
        summary = portfolio_mgr.portfolio_summary("fund-2")
        assert summary["asset_count"] == 4
        assert summary["total_value"] > 0
        assert summary["diversification_score"] > 0

        # Verify cap table
        fd = captable_mgr.calculate_fully_diluted("growth-ct")
        assert fd["shareholder_count"] == 3
        assert fd["total_shares_basic"] == 7_000_000

        # Waterfall at $20M exit
        waterfall = captable_mgr.waterfall_analysis("growth-ct", 20_000_000)
        total_paid = sum(r["payout"] for r in waterfall["waterfall"])
        assert abs(total_paid - 20_000_000) < 1.0

        # Verify preferred gets preference
        gp_row = next(r for r in waterfall["waterfall"] if r["name"] == "Growth Partners")
        assert gp_row["payout"] >= gp_row["investment"]

        # All subscription payments tracked
        payments = payment_svc.get_customer_payments("cus_seed_vc")
        assert len(payments) == 1
        assert payments[0].amount_cents == 500_000_00

        # Subscriptions
        payment_svc.create_subscription("sub_seed", "cus_seed_vc", "enterprise")
        payment_svc.create_subscription("sub_series_a", "cus_series_a", "enterprise")
        active = payment_svc.get_active_subscriptions("cus_seed_vc")
        assert len(active) == 1

    def test_portfolio_rebalance_after_investment(self, portfolio_mgr):
        portfolio_mgr.create("rebal", "Rebalance Test", "test@example.com")
        portfolio_mgr.add_asset("rebal", Asset("AAPL", 20, 150, 190, "equity", "technology"))
        portfolio_mgr.add_asset("rebal", Asset("BTC", 0.5, 40000, 68000, "crypto", "crypto"))
        portfolio_mgr.update_prices("rebal", {"AAPL": 190, "BTC": 68000})

        summary = portfolio_mgr.portfolio_summary("rebal")
        total = summary["total_value"]
        assert total > 0

        # Suggest rebalance to 50/50
        suggestions = portfolio_mgr.rebalance_suggestion("rebal", {"AAPL": 50, "BTC": 50})
        assert isinstance(suggestions, list)
        assert len(suggestions) > 0


class TestE2ESubscriptionLifecycle:
    """Full subscription lifecycle: create -> charge -> renew -> cancel."""

    def test_subscription_full_lifecycle(self, payment_svc):
        # Create subscription
        sub = payment_svc.create_subscription("sub_lifecycle", "cus_lifecycle", "growth")
        assert sub.status == "active"

        # Monthly payment comes through
        payment_svc.create_payment(
            "pay_month_1", "cus_lifecycle",
            PLAN_PRICES[PlanTier.GROWTH]["monthly"],
            description="Growth plan — month 1",
        )

        # Second month
        payment_svc.create_payment(
            "pay_month_2", "cus_lifecycle",
            PLAN_PRICES[PlanTier.GROWTH]["monthly"],
            description="Growth plan — month 2",
        )

        # Customer cancels
        payment_svc.cancel_subscription("sub_lifecycle")

        # Verify cancelled
        active = payment_svc.get_active_subscriptions("cus_lifecycle")
        assert len(active) == 0

        # Verify all payments still on record
        payments = payment_svc.get_customer_payments("cus_lifecycle")
        assert len(payments) == 2
        assert all(p.amount_cents == 9900 for p in payments)


class TestE2EPayoutRouting:
    """End-to-end payout routing to Raspberry Pi nodes."""

    def test_payout_routing_to_pi_fleet(self, payment_svc):
        # Configure Pi node fleet
        payment_svc.add_payout_route(
            destination="pi-primary.blackroad.local",
            destination_type="pi",
            percentage=50.0,
        )
        payment_svc.add_payout_route(
            destination="pi-secondary.blackroad.local",
            destination_type="pi",
            percentage=30.0,
        )
        payment_svc.add_payout_route(
            destination="pi-backup.blackroad.local",
            destination_type="pi",
            percentage=20.0,
        )

        routes = payment_svc.get_active_routes()
        assert len(routes) == 3
        assert sum(r.percentage for r in routes) == 100.0

        # Route payment
        results = payment_svc.route_payout(100_000_00, "Monthly infrastructure revenue")
        assert len(results) == 3
        assert results[0]["destination"] == "pi-primary.blackroad.local"

        total_routed = sum(r["amount_cents"] for r in results)
        assert total_routed == 100_000_00

    def test_invoice_webhook_triggers_payout(self, payment_svc):
        # Set up routes first
        payment_svc.add_payout_route("pi-main.local", "pi", percentage=100.0)

        # Simulate invoice.payment_succeeded webhook
        payload = json.dumps({
            "id": "evt_invoice_paid",
            "type": "invoice.payment_succeeded",
            "data": {"object": {
                "id": "in_123",
                "amount_paid": 49900,
                "customer": "cus_enterprise",
            }},
        }).encode()

        ok, msg = payment_svc.handle_webhook(payload, "", "")
        assert ok is True


class TestE2EWebhookIntegration:
    """Full webhook processing pipeline."""

    def test_webhook_event_deduplication(self, payment_svc):
        payload = json.dumps({
            "id": "evt_dedup_e2e",
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": "pi_dedup"}},
        }).encode()

        ok1, msg1 = payment_svc.handle_webhook(payload, "", "")
        ok2, msg2 = payment_svc.handle_webhook(payload, "", "")

        assert ok1 is True
        assert ok2 is True
        assert "Already processed" in msg2

    def test_webhook_updates_payment_state(self, payment_svc):
        # Create a payment
        payment_svc.create_payment("e2e_pay_wh", "cus_wh", 25000)
        payment_svc.ledger.update_payment_status("e2e_pay_wh", "processing", "pi_e2e_wh")

        # Success webhook
        payload = json.dumps({
            "id": "evt_e2e_success",
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": "pi_e2e_wh"}},
        }).encode()
        payment_svc.handle_webhook(payload, "", "")

        payment = payment_svc.get_payment("e2e_pay_wh")
        assert payment.status == PaymentStatus.SUCCEEDED

    def test_webhook_subscription_lifecycle(self, payment_svc):
        # Subscription created via webhook
        create_payload = json.dumps({
            "id": "evt_sub_e2e_create",
            "type": "customer.subscription.created",
            "data": {"object": {
                "id": "sub_e2e_stripe",
                "customer": "cus_e2e_sub",
                "plan": {"id": "price_growth"},
            }},
        }).encode()
        payment_svc.handle_webhook(create_payload, "", "")

        sub = payment_svc.ledger.get_subscription("sub_e2e_stripe")
        assert sub is not None
        assert sub.status == SubscriptionStatus.ACTIVE

        # Subscription cancelled via webhook
        cancel_payload = json.dumps({
            "id": "evt_sub_e2e_cancel",
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": "sub_e2e_stripe"}},
        }).encode()
        payment_svc.handle_webhook(cancel_payload, "", "")

        sub = payment_svc.ledger.get_subscription("sub_e2e_stripe")
        assert sub.status == SubscriptionStatus.CANCELLED


class TestE2EAnalyticsAfterPayments:
    """Verify analytics remain correct after payment flows."""

    def test_portfolio_analytics_after_investment_flow(self, portfolio_mgr, payment_svc):
        # Payment recorded
        payment_svc.create_payment("analytics_pay", "cus_analytics", 50_000_00)

        # Create portfolio with diverse assets
        portfolio_mgr.create("analytics-fund", "Analytics Test Fund", "test@blackroad.io")
        assets = [
            Asset("BTC", 0.5, 42000, 68000, "crypto", "crypto"),
            Asset("ETH", 5.0, 2200, 3800, "crypto", "crypto"),
            Asset("NVDA", 10, 450, 875, "equity", "technology"),
            Asset("SPY", 5, 400, 510, "etf", "diversified"),
            Asset("TLT", 10, 100, 88, "bond", "fixed_income"),
        ]
        for a in assets:
            portfolio_mgr.add_asset("analytics-fund", a)
            portfolio_mgr.update_prices("analytics-fund", {a.ticker: a.current_price})

        summary = portfolio_mgr.portfolio_summary("analytics-fund")

        # Verify all analytics populated
        assert summary["total_value"] > 0
        assert summary["total_cost"] > 0
        assert "diversification_score" in summary
        assert summary["diversification_score"] > 0
        assert "type_allocation" in summary
        assert "crypto" in summary["type_allocation"]
        assert "equity" in summary["type_allocation"]
        assert len(summary["top_performers"]) > 0
        assert len(summary["bottom_performers"]) > 0

        # Returns analysis
        returns = portfolio_mgr.calculate_returns("analytics-fund")
        assert "_aggregate" in returns
        assert returns["_aggregate"]["total_pnl"] > 0

    def test_cap_table_analytics_after_investment_flow(self, captable_mgr, payment_svc):
        # Payment recorded
        payment_svc.create_payment("cap_pay", "cus_cap", 1_000_000_00)

        # Set up cap table
        captable_mgr.create("analytics-ct", "Analytics Corp")
        captable_mgr.add_shareholder(
            "analytics-ct",
            Shareholder("Founder", "founder", 5_000_000, "common", 0.001),
        )
        captable_mgr.add_shareholder(
            "analytics-ct",
            Shareholder("Seed VC", "institution", 1_000_000, "preferred_a", 1.00),
        )
        captable_mgr.add_shareholder(
            "analytics-ct",
            Shareholder("Series A VC", "institution", 2_000_000, "preferred_b", 3.00),
        )

        # Ownership sums to 100%
        ownership = captable_mgr.get_ownership_pct("analytics-ct")
        total_pct = sum(o["ownership_pct"] for o in ownership)
        assert abs(total_pct - 100.0) < 0.1

        # Fully diluted metrics
        fd = captable_mgr.calculate_fully_diluted("analytics-ct")
        assert fd["total_shares_basic"] == 8_000_000
        assert fd["total_raised"] > 0
        assert fd["implied_valuation"] > 0

        # Waterfall at $50M exit
        waterfall = captable_mgr.waterfall_analysis("analytics-ct", 50_000_000)
        total_distributed = sum(r["payout"] for r in waterfall["waterfall"])
        assert abs(total_distributed - 50_000_000) < 1.0

        # Preferred gets liquidation preference
        series_a = next(r for r in waterfall["waterfall"] if r["name"] == "Series A VC")
        assert series_a["payout"] >= series_a["investment"]
        assert series_a["moic"] >= 1.0
