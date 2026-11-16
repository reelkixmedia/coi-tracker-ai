"""Vendor status from the current certificate + the customer's requirements.

Statuses (exactly the five in the build prompt):
  needs_review - the certificate is still being read, failed to read, or has low-confidence fields nobody confirmed
  expired      - a tracked policy's expiration date is before today
  missing      - no certificate, a required policy is absent, a limit is under the requirement, not additional
                 insured when required, or the holder name doesn't match
  expiring     - a tracked policy expires within EXPIRING_WINDOW_DAYS (30)
  compliant    - none of the above
Order when several apply: needs_review > expired > missing > expiring > compliant.
Tracked policies = the policy types the requirements ask for; with no requirement on a type, any policy of that
type that is on the certificate is still tracked for expiration.
This compares dates and numbers to the customer's own settings. It is not advice on whether coverage is adequate.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import utcnow
from .fields import POLICY_TYPES, as_date, as_int
from .models import DEFAULT_REQUIREMENTS, Account, Certificate, Vendor

STATUS_LABELS = {
    "compliant": "Compliant",
    "expiring": "Expiring in 30 days",
    "expired": "Expired",
    "missing": "Missing requirement",
    "needs_review": "Needs review",
}
STATUS_ORDER = ["expired", "expiring", "missing", "needs_review", "compliant"]
TYPE_LABEL = dict(POLICY_TYPES)


def local_today(account: Account, now: Optional[datetime] = None) -> date:
    now = now or utcnow()
    try:
        tz = ZoneInfo(account.timezone or "America/New_York")
    except Exception:
        tz = ZoneInfo("America/New_York")
    return now.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).date()


def requirements_for(account: Account) -> dict:
    req = dict(DEFAULT_REQUIREMENTS)
    req.update(account.requirements or {})
    return req


def cert_values(cert: Certificate) -> dict:
    """What the status uses: the human-confirmed values when there are any, else the extracted values."""
    vals = {k: (v or {}).get("value", "") for k, v in (cert.extracted or {}).items()}
    vals.update(cert.confirmed or {})
    return vals


def required_types(req: dict) -> set[str]:
    out = set()
    if as_int(req.get("gl_each_occurrence")) or as_int(req.get("gl_general_aggregate")) or req.get(
            "additional_insured_required"):
        out.add("gl")
    if as_int(req.get("auto_combined_single_limit")):
        out.add("auto")
    if req.get("wc_required"):
        out.add("wc")
    if as_int(req.get("umbrella_each_occurrence")):
        out.add("umbrella")
    return out


def evaluate(vals: dict, req: dict, today: date) -> tuple[str, list[str], Optional[date]]:
    """Pure function: (status, reasons, next_expiration) for one certificate's values."""
    def present(t):
        return bool(vals.get(f"{t}.policy_number") or vals.get(f"{t}.expiration_date"))

    need = required_types(req)
    tracked = need | {t for t, _ in POLICY_TYPES if present(t)}
    expired, expiring, missing = [], [], []
    exps = []
    window = get_settings().expiring_window_days
    for t in sorted(tracked):
        label = TYPE_LABEL[t]
        if not present(t):
            missing.append(f"{label} not on the certificate")
            continue
        exp = as_date(vals.get(f"{t}.expiration_date", ""))
        if exp is None:
            missing.append(f"{label}: no expiration date")
            continue
        exps.append(exp)
        if exp < today:
            expired.append(f"{label} expired {exp:%b %d, %Y}")
        elif exp <= today + timedelta(days=window):
            expiring.append(f"{label} expires {exp:%b %d, %Y} ({(exp - today).days} days)")

    def under(key, req_key, label):
        want = as_int(req.get(req_key))
        have = as_int(vals.get(key))
        if want and present(key.split(".")[0]) and have < want:
            missing.append(f"{label} ${have:,} is under your ${want:,}")

    under("gl.each_occurrence", "gl_each_occurrence", "GL each occurrence")
    under("gl.general_aggregate", "gl_general_aggregate", "GL aggregate")
    under("auto.combined_single_limit", "auto_combined_single_limit", "Auto combined single limit")
    under("umbrella.each_occurrence", "umbrella_each_occurrence", "Umbrella each occurrence")
    if req.get("additional_insured_required") and present("gl") and vals.get("gl.additional_insured") != "yes":
        missing.append("You are not marked as additional insured on GL")
    holder = (req.get("holder_name_contains") or "").strip().lower()
    if holder and holder not in (vals.get("certificate_holder") or "").lower():
        missing.append(f"Certificate holder doesn't include \"{req['holder_name_contains']}\"")

    nxt = min(exps) if exps else None
    if expired:
        return "expired", expired + missing, nxt
    if missing:
        return "missing", missing + expiring, nxt
    if expiring:
        return "expiring", expiring, nxt
    return "compliant", [], nxt


def current_certificate(session: Session, vendor: Vendor) -> Optional[Certificate]:
    """Newest certificate for the vendor (any state)."""
    return session.scalar(select(Certificate).where(Certificate.vendor_id == vendor.id)
                          .order_by(Certificate.created_at.desc(), Certificate.id.desc()).limit(1))


def best_certificate(session: Session, vendor: Vendor) -> Optional[Certificate]:
    """The certificate status is based on: newest one that finished reading (falls back to newest overall)."""
    done = session.scalar(select(Certificate).where(Certificate.vendor_id == vendor.id,
                                                    Certificate.extraction_status == "done")
                          .order_by(Certificate.created_at.desc(), Certificate.id.desc()).limit(1))
    return done or current_certificate(session, vendor)


def recompute_vendor(session: Session, account: Account, vendor: Vendor, now: Optional[datetime] = None) -> str:
    today = local_today(account, now)
    cert = best_certificate(session, vendor)
    newest = current_certificate(session, vendor)
    if cert is None:
        status, reasons, nxt = "missing", ["No certificate on file"], None
    else:
        status, reasons, nxt = evaluate(cert_values(cert), requirements_for(account), today)
        if cert.extraction_status in ("queued", "processing"):
            status, reasons = "needs_review", ["Reading the certificate..."] + reasons
        elif cert.extraction_status == "failed":
            status, reasons = "needs_review", [f"Couldn't read the certificate: {cert.extraction_error}"]
        elif cert.review_state == "needs_review":
            low = cert.low_confidence_fields or []
            status, reasons = "needs_review", [f"{len(low)} field(s) need a quick check"] + reasons
    if newest is not None and cert is not None and newest.id != cert.id and newest.extraction_status in (
            "queued", "processing"):
        reasons = ["A newer certificate is being read"] + list(reasons)
    vendor.status = status
    vendor.status_reasons = reasons
    vendor.next_expiration = nxt
    vendor.current_certificate_id = cert.id if cert else None
    vendor.status_computed_at = utcnow()
    return status


def recompute_account(session: Session, account: Account, now: Optional[datetime] = None) -> dict:
    counts = {k: 0 for k in STATUS_LABELS}
    for v in session.scalars(select(Vendor).where(Vendor.account_id == account.id, Vendor.active.is_(True))).all():
        counts[recompute_vendor(session, account, v, now)] += 1
    return counts
