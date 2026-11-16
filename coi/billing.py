"""Plans + Stripe, following the Recovery Engine's app/billing.py pattern (Checkout, portal, idempotent webhooks
recorded in stripe_events). Tiers and prices are config only (PLAN_TIERS_JSON); nothing is priced in code.

Free scan: upload up to FREE_SCAN_MAX_COIS certificates and see the dashboard. Renewal reminders and bulk upload
need a paid plan (FREE_BULK_UPLOAD=true opens bulk to free accounts).
Local testing without Stripe: with DRY_RUN=true and no STRIPE_SECRET_KEY, /billing shows "Activate test plan".
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .eventlog import log_event
from .models import Account, Certificate, StripeEventSeen, User, Vendor

STATUS = {"active": "active", "trialing": "active", "past_due": "past_due", "unpaid": "past_due",
          "paused": "past_due", "canceled": "canceled", "incomplete_expired": "canceled"}


def is_paid(account: Account) -> bool:
    return account.plan_status == "active"


def tier(key: str) -> Optional[dict]:
    return next((t for t in get_settings().plan_tiers() if t["key"] == key), None)


def cert_count(session: Session, account: Account) -> int:
    return session.scalar(select(func.count(Certificate.id)).where(Certificate.account_id == account.id)) or 0


def upload_allowance(session: Session, account: Account) -> Optional[int]:
    """How many more certificates this account may upload (None = no limit)."""
    if is_paid(account):
        return None
    return max(0, get_settings().free_scan_max_cois - cert_count(session, account))


def can_bulk(account: Account) -> bool:
    return is_paid(account) or get_settings().free_bulk_upload


def vendor_cap(account: Account) -> int:
    t = tier(account.plan_key) if is_paid(account) else None
    return int((t or {}).get("max_vendors") or 0)


def vendor_count(session: Session, account: Account) -> int:
    return session.scalar(select(func.count(Vendor.id)).where(Vendor.account_id == account.id,
                                                              Vendor.active.is_(True))) or 0


def dev_activation_allowed() -> bool:
    s = get_settings()
    return s.dry_run and not s.stripe_secret_key


def _stripe():
    import stripe

    stripe.api_key = get_settings().stripe_secret_key
    return stripe


def checkout_url(account: Account, email: str, plan_key: str) -> Optional[str]:
    t = tier(plan_key)
    s = get_settings()
    if not t or not t.get("stripe_price") or not s.stripe_secret_key:
        return None
    base = s.base_url.rstrip("/")
    meta = {"account_id": str(account.id), "plan": plan_key}
    params = dict(mode="subscription", line_items=[{"price": t["stripe_price"], "quantity": 1}],
                  client_reference_id=str(account.id), success_url=f"{base}/billing?done=1",
                  cancel_url=f"{base}/billing", allow_promotion_codes=True, metadata=meta,
                  subscription_data={"metadata": meta})
    if account.stripe_customer_id:
        params["customer"] = account.stripe_customer_id
    else:
        params["customer_email"] = email
    return _stripe().checkout.Session.create(**params).url


def portal_url(account: Account) -> Optional[str]:
    if not (get_settings().stripe_secret_key and account.stripe_customer_id):
        return None
    return _stripe().billing_portal.Session.create(customer=account.stripe_customer_id,
                                                   return_url=get_settings().base_url.rstrip("/") + "/billing").url


def _g(obj, *path, default=None):
    cur = obj
    for key in path:
        try:
            cur = cur[key]
        except (KeyError, IndexError, TypeError):
            return default
        if cur is None:
            return default
    return cur


def handle_event(db: Session, event: dict) -> Optional[str]:
    """Stripe webhook (signature already verified). Idempotent per event id."""
    eid = event.get("id", "")
    if eid and db.get(StripeEventSeen, eid):
        return "duplicate"
    if eid:
        db.add(StripeEventSeen(id=eid))
    typ = event.get("type", "")
    obj = _g(event, "data", "object", default={}) or {}
    account = None
    if typ == "checkout.session.completed":
        aid = _g(obj, "metadata", "account_id") or obj.get("client_reference_id")
        account = db.get(Account, int(aid)) if aid and str(aid).isdigit() else None
        if account is None and _g(obj, "customer_details", "email"):
            u = db.scalar(select(User).where(User.email == obj["customer_details"]["email"].lower()))
            account = db.get(Account, u.account_id) if u else None
        if account is None:
            return "no_account"
        account.stripe_customer_id = obj.get("customer") or account.stripe_customer_id
        account.stripe_subscription_id = obj.get("subscription") or account.stripe_subscription_id
        account.plan_key = _g(obj, "metadata", "plan") or account.plan_key
        account.plan_status = "active"
        log_event(db, "stripe_checkout_completed", account.id, plan=account.plan_key)
        return "activated"
    if typ in ("customer.subscription.updated", "customer.subscription.deleted", "customer.subscription.created"):
        account = db.scalar(select(Account).where(Account.stripe_subscription_id == obj.get("id"))) or (
            db.scalar(select(Account).where(Account.stripe_customer_id == obj.get("customer"))))
        if account is None:
            return "no_account"
        account.plan_status = STATUS.get(obj.get("status", ""), account.plan_status)
        if typ == "customer.subscription.deleted":
            account.plan_status = "canceled"
        log_event(db, "stripe_subscription_" + typ.rsplit(".", 1)[-1], account.id, status=account.plan_status)
        return account.plan_status
    if typ == "invoice.payment_failed":
        account = db.scalar(select(Account).where(Account.stripe_customer_id == obj.get("customer")))
        if account is not None:
            account.plan_status = "past_due"
            log_event(db, "stripe_payment_failed", account.id)
        return "past_due"
    return "ignored"
