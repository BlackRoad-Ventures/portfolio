#!/usr/bin/env python3
"""
BlackRoad Ventures — Stripe Payment Integration
=================================================
Production Stripe integration for portfolio subscriptions, one-time payments,
webhook processing, and payout routing to connected accounts.

Supports:
    - Subscription plans (investor tiers)
    - One-time deal payments
    - Webhook signature verification + event dispatch
    - Payout routing to Pi-hosted connected accounts
    - Idempotent payment processing with SQLite ledger

Environment:
    STRIPE_SECRET_KEY       - Stripe API secret key
    STRIPE_WEBHOOK_SECRET   - Webhook endpoint signing secret
    STRIPE_CONNECT_ACCOUNT  - Optional connected account for payouts
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# ─── Configuration ────────────────────────────────────────────────────────────

STRIPE_API_BASE = "https://api.stripe.com/v1"
DB_PATH = Path.home() / ".blackroad" / "stripe_ledger.db"

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    id                TEXT PRIMARY KEY,
    stripe_payment_id TEXT UNIQUE,
    customer_id       TEXT NOT NULL,
    amount_cents      INTEGER NOT NULL,
    currency          TEXT NOT NULL DEFAULT 'usd',
    status            TEXT NOT NULL DEFAULT 'pending',
    payment_type      TEXT NOT NULL DEFAULT 'one_time',
    description       TEXT NOT NULL DEFAULT '',
    metadata_json     TEXT NOT NULL DEFAULT '{}',
    idempotency_key   TEXT UNIQUE,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id                  TEXT PRIMARY KEY,
    stripe_sub_id       TEXT UNIQUE,
    customer_id         TEXT NOT NULL,
    plan_id             TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active',
    current_period_end  TEXT,
    cancel_at           TEXT,
    metadata_json       TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_events (
    id              TEXT PRIMARY KEY,
    stripe_event_id TEXT UNIQUE NOT NULL,
    event_type      TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    processed       INTEGER NOT NULL DEFAULT 0,
    processed_at    TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payout_routes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    destination         TEXT NOT NULL,
    destination_type    TEXT NOT NULL DEFAULT 'pi',
    connected_account   TEXT,
    percentage          REAL NOT NULL DEFAULT 100.0,
    active              INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_payments_customer ON payments (customer_id);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments (status);
CREATE INDEX IF NOT EXISTS idx_subs_customer ON subscriptions (customer_id);
CREATE INDEX IF NOT EXISTS idx_webhook_processed ON webhook_events (processed);
CREATE INDEX IF NOT EXISTS idx_payout_active ON payout_routes (active);
"""


# ─── Enums ────────────────────────────────────────────────────────────────────


class PaymentStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"


class SubscriptionStatus(str, Enum):
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELLED = "cancelled"
    TRIALING = "trialing"
    PAUSED = "paused"


class PlanTier(str, Enum):
    STARTER = "starter"
    GROWTH = "growth"
    ENTERPRISE = "enterprise"


PLAN_PRICES = {
    PlanTier.STARTER: {"monthly": 2900, "yearly": 29000},       # $29/mo
    PlanTier.GROWTH: {"monthly": 9900, "yearly": 99000},        # $99/mo
    PlanTier.ENTERPRISE: {"monthly": 49900, "yearly": 499000},  # $499/mo
}


# ─── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class Payment:
    id: str
    customer_id: str
    amount_cents: int
    currency: str = "usd"
    status: str = "pending"
    payment_type: str = "one_time"
    description: str = ""
    stripe_payment_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def amount_dollars(self) -> float:
        return self.amount_cents / 100.0

    @property
    def is_complete(self) -> bool:
        return self.status == PaymentStatus.SUCCEEDED


@dataclass
class Subscription:
    id: str
    customer_id: str
    plan_id: str
    status: str = "active"
    stripe_sub_id: Optional[str] = None
    current_period_end: Optional[str] = None
    cancel_at: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WebhookEvent:
    stripe_event_id: str
    event_type: str
    payload: Dict[str, Any]
    processed: bool = False


@dataclass
class PayoutRoute:
    destination: str
    destination_type: str = "pi"
    connected_account: Optional[str] = None
    percentage: float = 100.0
    active: bool = True


# ─── Stripe API client ───────────────────────────────────────────────────────


class StripeClient:
    """Minimal Stripe API client using stdlib only (no stripe-python dep)."""

    def __init__(self, secret_key: Optional[str] = None):
        self.secret_key = secret_key or os.environ.get("STRIPE_SECRET_KEY", "")
        if not self.secret_key:
            raise ValueError(
                "Stripe secret key required. Set STRIPE_SECRET_KEY env var "
                "or pass secret_key to StripeClient."
            )

    def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = f"{STRIPE_API_BASE}/{endpoint.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        body = urlencode(data).encode() if data else None
        req = Request(url, data=body, headers=headers, method=method)

        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            error_body = e.read().decode() if e.fp else ""
            raise StripeAPIError(
                f"Stripe API error {e.code}: {error_body}",
                status_code=e.code,
                body=error_body,
            ) from e

    # ── Customers ─────────────────────────────────────────────────────────────

    def create_customer(
        self, email: str, name: str, metadata: Optional[Dict] = None
    ) -> Dict:
        data = {"email": email, "name": name}
        if metadata:
            for k, v in metadata.items():
                data[f"metadata[{k}]"] = str(v)
        return self._request("POST", "customers", data)

    def get_customer(self, customer_id: str) -> Dict:
        return self._request("GET", f"customers/{customer_id}")

    # ── Payment Intents ───────────────────────────────────────────────────────

    def create_payment_intent(
        self,
        amount_cents: int,
        currency: str = "usd",
        customer_id: Optional[str] = None,
        description: str = "",
        metadata: Optional[Dict] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict:
        data: Dict[str, Any] = {
            "amount": amount_cents,
            "currency": currency,
            "description": description,
        }
        if customer_id:
            data["customer"] = customer_id
        if metadata:
            for k, v in metadata.items():
                data[f"metadata[{k}]"] = str(v)
        return self._request(
            "POST", "payment_intents", data, idempotency_key=idempotency_key
        )

    def confirm_payment_intent(self, payment_intent_id: str) -> Dict:
        return self._request(
            "POST", f"payment_intents/{payment_intent_id}/confirm"
        )

    def get_payment_intent(self, payment_intent_id: str) -> Dict:
        return self._request("GET", f"payment_intents/{payment_intent_id}")

    # ── Subscriptions ─────────────────────────────────────────────────────────

    def create_subscription(
        self,
        customer_id: str,
        price_id: str,
        metadata: Optional[Dict] = None,
    ) -> Dict:
        data: Dict[str, Any] = {
            "customer": customer_id,
            "items[0][price]": price_id,
        }
        if metadata:
            for k, v in metadata.items():
                data[f"metadata[{k}]"] = str(v)
        return self._request("POST", "subscriptions", data)

    def cancel_subscription(self, subscription_id: str) -> Dict:
        return self._request("DELETE", f"subscriptions/{subscription_id}")

    def get_subscription(self, subscription_id: str) -> Dict:
        return self._request("GET", f"subscriptions/{subscription_id}")

    # ── Payouts / Transfers ───────────────────────────────────────────────────

    def create_transfer(
        self,
        amount_cents: int,
        destination_account: str,
        currency: str = "usd",
        description: str = "",
    ) -> Dict:
        data = {
            "amount": amount_cents,
            "currency": currency,
            "destination": destination_account,
            "description": description,
        }
        return self._request("POST", "transfers", data)

    # ── Webhook verification ──────────────────────────────────────────────────

    @staticmethod
    def verify_webhook_signature(
        payload: bytes,
        sig_header: str,
        webhook_secret: str,
        tolerance: int = 300,
    ) -> bool:
        """
        Verify Stripe webhook signature (v1 scheme).
        Returns True if signature is valid and timestamp within tolerance.
        """
        try:
            parts = dict(
                item.split("=", 1) for item in sig_header.split(",")
            )
            timestamp = parts.get("t", "")
            signature = parts.get("v1", "")

            if not timestamp or not signature:
                return False

            # Check timestamp tolerance
            ts = int(timestamp)
            if abs(time.time() - ts) > tolerance:
                return False

            # Compute expected signature
            signed_payload = f"{timestamp}.".encode() + payload
            expected = hmac.new(
                webhook_secret.encode(),
                signed_payload,
                hashlib.sha256,
            ).hexdigest()

            return hmac.compare_digest(expected, signature)
        except (ValueError, KeyError):
            return False


class StripeAPIError(Exception):
    def __init__(self, message: str, status_code: int = 0, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


# ─── Payment ledger (SQLite persistence) ─────────────────────────────────────


class PaymentLedger:
    """Local ledger tracking all payments and subscriptions with idempotency."""

    def __init__(self, db_path: Path = DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(LEDGER_SCHEMA)
        self.conn.commit()

    def _now(self) -> str:
        return datetime.utcnow().isoformat()

    # ── Payments ──────────────────────────────────────────────────────────────

    def record_payment(self, payment: Payment) -> Payment:
        now = self._now()
        self.conn.execute(
            """INSERT OR IGNORE INTO payments
               (id, stripe_payment_id, customer_id, amount_cents, currency,
                status, payment_type, description, metadata_json,
                idempotency_key, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                payment.id, payment.stripe_payment_id, payment.customer_id,
                payment.amount_cents, payment.currency, payment.status,
                payment.payment_type, payment.description,
                json.dumps(payment.metadata), payment.idempotency_key,
                now, now,
            ),
        )
        self.conn.commit()
        return payment

    def update_payment_status(
        self, payment_id: str, status: str, stripe_payment_id: Optional[str] = None
    ) -> None:
        now = self._now()
        if stripe_payment_id:
            self.conn.execute(
                "UPDATE payments SET status=?, stripe_payment_id=?, updated_at=? WHERE id=?",
                (status, stripe_payment_id, now, payment_id),
            )
        else:
            self.conn.execute(
                "UPDATE payments SET status=?, updated_at=? WHERE id=?",
                (status, now, payment_id),
            )
        self.conn.commit()

    def get_payment(self, payment_id: str) -> Optional[Payment]:
        row = self.conn.execute(
            "SELECT * FROM payments WHERE id=?", (payment_id,)
        ).fetchone()
        if not row:
            return None
        return Payment(
            id=row["id"],
            customer_id=row["customer_id"],
            amount_cents=row["amount_cents"],
            currency=row["currency"],
            status=row["status"],
            payment_type=row["payment_type"],
            description=row["description"],
            stripe_payment_id=row["stripe_payment_id"],
            idempotency_key=row["idempotency_key"],
            metadata=json.loads(row["metadata_json"]),
        )

    def get_customer_payments(self, customer_id: str) -> List[Payment]:
        rows = self.conn.execute(
            "SELECT * FROM payments WHERE customer_id=? ORDER BY created_at DESC",
            (customer_id,),
        ).fetchall()
        return [
            Payment(
                id=r["id"], customer_id=r["customer_id"],
                amount_cents=r["amount_cents"], currency=r["currency"],
                status=r["status"], payment_type=r["payment_type"],
                description=r["description"],
                stripe_payment_id=r["stripe_payment_id"],
                idempotency_key=r["idempotency_key"],
                metadata=json.loads(r["metadata_json"]),
            )
            for r in rows
        ]

    def check_idempotency(self, key: str) -> Optional[Payment]:
        row = self.conn.execute(
            "SELECT * FROM payments WHERE idempotency_key=?", (key,)
        ).fetchone()
        if not row:
            return None
        return Payment(
            id=row["id"], customer_id=row["customer_id"],
            amount_cents=row["amount_cents"], currency=row["currency"],
            status=row["status"], payment_type=row["payment_type"],
            description=row["description"],
            stripe_payment_id=row["stripe_payment_id"],
            idempotency_key=row["idempotency_key"],
            metadata=json.loads(row["metadata_json"]),
        )

    # ── Subscriptions ─────────────────────────────────────────────────────────

    def record_subscription(self, sub: Subscription) -> Subscription:
        now = self._now()
        self.conn.execute(
            """INSERT OR IGNORE INTO subscriptions
               (id, stripe_sub_id, customer_id, plan_id, status,
                current_period_end, cancel_at, metadata_json,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                sub.id, sub.stripe_sub_id, sub.customer_id, sub.plan_id,
                sub.status, sub.current_period_end, sub.cancel_at,
                json.dumps(sub.metadata), now, now,
            ),
        )
        self.conn.commit()
        return sub

    def update_subscription_status(self, sub_id: str, status: str) -> None:
        now = self._now()
        self.conn.execute(
            "UPDATE subscriptions SET status=?, updated_at=? WHERE id=?",
            (status, now, sub_id),
        )
        self.conn.commit()

    def get_subscription(self, sub_id: str) -> Optional[Subscription]:
        row = self.conn.execute(
            "SELECT * FROM subscriptions WHERE id=?", (sub_id,)
        ).fetchone()
        if not row:
            return None
        return Subscription(
            id=row["id"], customer_id=row["customer_id"],
            plan_id=row["plan_id"], status=row["status"],
            stripe_sub_id=row["stripe_sub_id"],
            current_period_end=row["current_period_end"],
            cancel_at=row["cancel_at"],
            metadata=json.loads(row["metadata_json"]),
        )

    def get_active_subscriptions(self, customer_id: str) -> List[Subscription]:
        rows = self.conn.execute(
            "SELECT * FROM subscriptions WHERE customer_id=? AND status='active' "
            "ORDER BY created_at DESC",
            (customer_id,),
        ).fetchall()
        return [
            Subscription(
                id=r["id"], customer_id=r["customer_id"],
                plan_id=r["plan_id"], status=r["status"],
                stripe_sub_id=r["stripe_sub_id"],
                current_period_end=r["current_period_end"],
                cancel_at=r["cancel_at"],
                metadata=json.loads(r["metadata_json"]),
            )
            for r in rows
        ]

    # ── Webhook events ────────────────────────────────────────────────────────

    def record_webhook_event(self, event: WebhookEvent) -> bool:
        """Record a webhook event. Returns False if already exists (dedup)."""
        now = self._now()
        try:
            self.conn.execute(
                """INSERT INTO webhook_events
                   (id, stripe_event_id, event_type, payload_json, processed, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    event.stripe_event_id, event.stripe_event_id,
                    event.event_type, json.dumps(event.payload),
                    0, now,
                ),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # Already processed

    def mark_event_processed(self, event_id: str) -> None:
        now = self._now()
        self.conn.execute(
            "UPDATE webhook_events SET processed=1, processed_at=? WHERE stripe_event_id=?",
            (now, event_id),
        )
        self.conn.commit()

    def get_unprocessed_events(self) -> List[WebhookEvent]:
        rows = self.conn.execute(
            "SELECT * FROM webhook_events WHERE processed=0 ORDER BY created_at"
        ).fetchall()
        return [
            WebhookEvent(
                stripe_event_id=r["stripe_event_id"],
                event_type=r["event_type"],
                payload=json.loads(r["payload_json"]),
                processed=bool(r["processed"]),
            )
            for r in rows
        ]

    # ── Payout routes ─────────────────────────────────────────────────────────

    def add_payout_route(self, route: PayoutRoute) -> int:
        now = self._now()
        cur = self.conn.execute(
            """INSERT INTO payout_routes
               (destination, destination_type, connected_account, percentage, active, created_at)
               VALUES (?,?,?,?,?,?)""",
            (
                route.destination, route.destination_type,
                route.connected_account, route.percentage,
                int(route.active), now,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_active_routes(self) -> List[PayoutRoute]:
        rows = self.conn.execute(
            "SELECT * FROM payout_routes WHERE active=1 ORDER BY percentage DESC"
        ).fetchall()
        return [
            PayoutRoute(
                destination=r["destination"],
                destination_type=r["destination_type"],
                connected_account=r["connected_account"],
                percentage=r["percentage"],
                active=bool(r["active"]),
            )
            for r in rows
        ]

    def close(self) -> None:
        self.conn.close()


# ─── Webhook processor ───────────────────────────────────────────────────────


class WebhookProcessor:
    """Dispatches Stripe webhook events to registered handlers."""

    def __init__(self, ledger: PaymentLedger):
        self.ledger = ledger
        self._handlers: Dict[str, List[Callable]] = {}

    def register(self, event_type: str, handler: Callable) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def process_event(self, event: WebhookEvent) -> bool:
        """Process a single webhook event. Returns True if handled."""
        is_new = self.ledger.record_webhook_event(event)
        if not is_new:
            return False  # Already processed (idempotent)

        handlers = self._handlers.get(event.event_type, [])
        for handler in handlers:
            handler(event)

        self.ledger.mark_event_processed(event.stripe_event_id)
        return True


# ─── Payment service (high-level facade) ─────────────────────────────────────


class PaymentService:
    """
    High-level payment service combining Stripe API + local ledger.
    This is the main entry point for application code.
    """

    def __init__(
        self,
        stripe_client: Optional[StripeClient] = None,
        ledger: Optional[PaymentLedger] = None,
        db_path: Path = DB_PATH,
    ):
        self.stripe = stripe_client
        self.ledger = ledger or PaymentLedger(db_path)
        self.webhook_processor = WebhookProcessor(self.ledger)
        self._register_default_handlers()

    def _register_default_handlers(self) -> None:
        self.webhook_processor.register(
            "payment_intent.succeeded", self._handle_payment_succeeded
        )
        self.webhook_processor.register(
            "payment_intent.payment_failed", self._handle_payment_failed
        )
        self.webhook_processor.register(
            "customer.subscription.created", self._handle_subscription_created
        )
        self.webhook_processor.register(
            "customer.subscription.deleted", self._handle_subscription_cancelled
        )
        self.webhook_processor.register(
            "customer.subscription.updated", self._handle_subscription_updated
        )
        self.webhook_processor.register(
            "invoice.payment_succeeded", self._handle_invoice_paid
        )

    # ── Payment operations ────────────────────────────────────────────────────

    def create_payment(
        self,
        payment_id: str,
        customer_id: str,
        amount_cents: int,
        currency: str = "usd",
        description: str = "",
        metadata: Optional[Dict] = None,
        idempotency_key: Optional[str] = None,
    ) -> Payment:
        """Create a payment in the ledger and optionally on Stripe."""
        # Idempotency check
        if idempotency_key:
            existing = self.ledger.check_idempotency(idempotency_key)
            if existing:
                return existing

        payment = Payment(
            id=payment_id,
            customer_id=customer_id,
            amount_cents=amount_cents,
            currency=currency,
            description=description,
            idempotency_key=idempotency_key,
            metadata=metadata or {},
        )
        self.ledger.record_payment(payment)

        # If Stripe client is configured, create on Stripe
        if self.stripe:
            try:
                intent = self.stripe.create_payment_intent(
                    amount_cents=amount_cents,
                    currency=currency,
                    customer_id=customer_id,
                    description=description,
                    metadata=metadata,
                    idempotency_key=idempotency_key,
                )
                payment.stripe_payment_id = intent["id"]
                payment.status = PaymentStatus.PROCESSING
                self.ledger.update_payment_status(
                    payment_id, PaymentStatus.PROCESSING, intent["id"]
                )
            except StripeAPIError:
                payment.status = PaymentStatus.FAILED
                self.ledger.update_payment_status(
                    payment_id, PaymentStatus.FAILED
                )

        return payment

    def get_payment(self, payment_id: str) -> Optional[Payment]:
        return self.ledger.get_payment(payment_id)

    def get_customer_payments(self, customer_id: str) -> List[Payment]:
        return self.ledger.get_customer_payments(customer_id)

    # ── Subscription operations ───────────────────────────────────────────────

    def create_subscription(
        self,
        sub_id: str,
        customer_id: str,
        plan_id: str,
        metadata: Optional[Dict] = None,
    ) -> Subscription:
        sub = Subscription(
            id=sub_id,
            customer_id=customer_id,
            plan_id=plan_id,
            metadata=metadata or {},
        )
        self.ledger.record_subscription(sub)
        return sub

    def cancel_subscription(self, sub_id: str) -> bool:
        sub = self.ledger.get_subscription(sub_id)
        if not sub:
            return False

        if self.stripe and sub.stripe_sub_id:
            try:
                self.stripe.cancel_subscription(sub.stripe_sub_id)
            except StripeAPIError:
                pass

        self.ledger.update_subscription_status(sub_id, SubscriptionStatus.CANCELLED)
        return True

    def get_active_subscriptions(self, customer_id: str) -> List[Subscription]:
        return self.ledger.get_active_subscriptions(customer_id)

    # ── Payout routing ────────────────────────────────────────────────────────

    def add_payout_route(
        self,
        destination: str,
        destination_type: str = "pi",
        connected_account: Optional[str] = None,
        percentage: float = 100.0,
    ) -> int:
        route = PayoutRoute(
            destination=destination,
            destination_type=destination_type,
            connected_account=connected_account,
            percentage=percentage,
        )
        return self.ledger.add_payout_route(route)

    def get_active_routes(self) -> List[PayoutRoute]:
        return self.ledger.get_active_routes()

    def route_payout(self, amount_cents: int, description: str = "") -> List[Dict]:
        """Route a payout to all active destinations proportionally."""
        routes = self.get_active_routes()
        if not routes:
            return []

        results = []
        for route in routes:
            share = int(amount_cents * route.percentage / 100.0)
            result = {
                "destination": route.destination,
                "destination_type": route.destination_type,
                "amount_cents": share,
                "description": description,
                "status": "routed",
            }

            if self.stripe and route.connected_account:
                try:
                    transfer = self.stripe.create_transfer(
                        amount_cents=share,
                        destination_account=route.connected_account,
                        description=description,
                    )
                    result["stripe_transfer_id"] = transfer["id"]
                    result["status"] = "transferred"
                except StripeAPIError as e:
                    result["status"] = "failed"
                    result["error"] = str(e)

            results.append(result)

        return results

    # ── Webhook handling ──────────────────────────────────────────────────────

    def handle_webhook(
        self,
        payload: bytes,
        sig_header: str,
        webhook_secret: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        Process an incoming Stripe webhook.
        Returns (success, message).
        """
        secret = webhook_secret or os.environ.get("STRIPE_WEBHOOK_SECRET", "")

        if secret:
            if not StripeClient.verify_webhook_signature(payload, sig_header, secret):
                return False, "Invalid webhook signature"

        try:
            event_data = json.loads(payload)
        except json.JSONDecodeError:
            return False, "Invalid JSON payload"

        event = WebhookEvent(
            stripe_event_id=event_data.get("id", ""),
            event_type=event_data.get("type", ""),
            payload=event_data,
        )

        if not event.stripe_event_id or not event.event_type:
            return False, "Missing event id or type"

        processed = self.webhook_processor.process_event(event)
        if processed:
            return True, f"Processed {event.event_type}"
        return True, f"Already processed {event.stripe_event_id}"

    # ── Default webhook handlers ──────────────────────────────────────────────

    def _handle_payment_succeeded(self, event: WebhookEvent) -> None:
        pi = event.payload.get("data", {}).get("object", {})
        stripe_id = pi.get("id", "")
        rows = self.ledger.conn.execute(
            "SELECT id FROM payments WHERE stripe_payment_id=?", (stripe_id,)
        ).fetchone()
        if rows:
            self.ledger.update_payment_status(rows["id"], PaymentStatus.SUCCEEDED)

    def _handle_payment_failed(self, event: WebhookEvent) -> None:
        pi = event.payload.get("data", {}).get("object", {})
        stripe_id = pi.get("id", "")
        rows = self.ledger.conn.execute(
            "SELECT id FROM payments WHERE stripe_payment_id=?", (stripe_id,)
        ).fetchone()
        if rows:
            self.ledger.update_payment_status(rows["id"], PaymentStatus.FAILED)

    def _handle_subscription_created(self, event: WebhookEvent) -> None:
        sub_obj = event.payload.get("data", {}).get("object", {})
        stripe_sub_id = sub_obj.get("id", "")
        customer_id = sub_obj.get("customer", "")
        plan_id = sub_obj.get("plan", {}).get("id", "")
        self.ledger.record_subscription(Subscription(
            id=stripe_sub_id,
            customer_id=customer_id,
            plan_id=plan_id,
            stripe_sub_id=stripe_sub_id,
            status=SubscriptionStatus.ACTIVE,
        ))

    def _handle_subscription_cancelled(self, event: WebhookEvent) -> None:
        sub_obj = event.payload.get("data", {}).get("object", {})
        stripe_sub_id = sub_obj.get("id", "")
        self.ledger.update_subscription_status(
            stripe_sub_id, SubscriptionStatus.CANCELLED
        )

    def _handle_subscription_updated(self, event: WebhookEvent) -> None:
        sub_obj = event.payload.get("data", {}).get("object", {})
        stripe_sub_id = sub_obj.get("id", "")
        status = sub_obj.get("status", "active")
        self.ledger.update_subscription_status(stripe_sub_id, status)

    def _handle_invoice_paid(self, event: WebhookEvent) -> None:
        invoice = event.payload.get("data", {}).get("object", {})
        amount = invoice.get("amount_paid", 0)
        customer = invoice.get("customer", "")
        if amount > 0:
            self.route_payout(
                amount,
                f"Invoice payment from {customer}",
            )

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def close(self) -> None:
        self.ledger.close()
