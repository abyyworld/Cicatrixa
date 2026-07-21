"""Stripe billing: Checkout for the $2.99/project/month subscription plus the
webhook that keeps `users.paid_until` current.

Raw REST via httpx (no SDK) — same pattern as gh.py / mailer.py. Configure with:
  STRIPE_SECRET_KEY      sk_live_... / sk_test_...
  STRIPE_WEBHOOK_SECRET  whsec_...   (from the dashboard webhook endpoint)
Without keys the subscribe buttons degrade to a contact link.
"""
import hashlib
import hmac
import json
import os
import time

import httpx

from . import db, referrals

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
API = "https://api.stripe.com/v1"
PRICE_CENTS = 299
PAID_CYCLE_DAYS = 32  # 30-day cycle + grace so renewals never flap


def available() -> bool:
    return bool(STRIPE_SECRET_KEY)


def _post(path: str, data: dict) -> dict:
    r = httpx.post(f"{API}{path}", data=data,
                   auth=(STRIPE_SECRET_KEY, ""), timeout=20)
    r.raise_for_status()
    return r.json()


def checkout_url(user, quantity: int, base_url: str) -> str:
    """Create a subscription Checkout Session and return its URL."""
    quantity = max(1, quantity)
    data = {
        "mode": "subscription",
        "client_reference_id": str(user["id"]),
        "customer_email": user["email"],
        "success_url": f"{base_url}/dashboard?paid=1",
        "cancel_url": f"{base_url}/dashboard",
        "line_items[0][quantity]": str(quantity),
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": str(PRICE_CENTS),
        "line_items[0][price_data][recurring][interval]": "month",
        "line_items[0][price_data][product_data][name]": "Cicatrixa hosting (per project)",
    }
    if user["stripe_customer_id"]:
        data["customer"] = user["stripe_customer_id"]
        del data["customer_email"]
    return _post("/checkout/sessions", data)["url"]


def verify_signature(payload: bytes, sig_header: str) -> bool:
    """Stripe-Signature: t=<ts>,v1=<hmac>. Reject stale (>5 min) or bad sigs."""
    if not STRIPE_WEBHOOK_SECRET or not sig_header:
        return False
    parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
    ts, sig = parts.get("t"), parts.get("v1")
    if not ts or not sig or abs(time.time() - int(ts)) > 300:
        return False
    signed = f"{ts}.".encode() + payload
    expected = hmac.new(STRIPE_WEBHOOK_SECRET.encode(), signed,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _extend_paid(user_id: int):
    base = max(db.one("SELECT paid_until FROM users WHERE id=?",
                      (user_id,))["paid_until"] or 0, db.now())
    db.q("UPDATE users SET paid_until=? WHERE id=?",
         (base + PAID_CYCLE_DAYS * 86400, user_id))
    referrals.record_conversion(user_id)


def handle_event(payload: bytes) -> str:
    """Process a verified webhook. Returns a short description for logs."""
    event = json.loads(payload)
    kind = event.get("type", "")
    obj = event.get("data", {}).get("object", {})

    if kind == "checkout.session.completed":
        uid = obj.get("client_reference_id")
        customer = obj.get("customer")
        if uid and str(uid).isdigit():
            if customer:
                db.q("UPDATE users SET stripe_customer_id=? WHERE id=?",
                     (customer, int(uid)))
            _extend_paid(int(uid))
            return f"activated user {uid}"

    elif kind == "invoice.paid":
        customer = obj.get("customer")
        if customer:
            user = db.one("SELECT id FROM users WHERE stripe_customer_id=?",
                          (customer,))
            if user:
                _extend_paid(user["id"])
                return f"renewed user {user['id']}"

    return f"ignored {kind}"
