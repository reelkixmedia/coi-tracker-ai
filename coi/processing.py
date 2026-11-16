"""Upload -> store -> read -> match to a vendor -> review state -> renewal check -> status. No clicks in between.

store_upload() is called by the web request (fast). process_certificate() runs right after in a background task
(and the worker retries anything left queued), so uploads from the dashboard and from a sub's link both end
with an up-to-date status by themselves.
"""
from __future__ import annotations

import difflib
import hashlib
import os
import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import extraction, status
from .config import get_settings
from .db import session_scope, utcnow
from .eventlog import log_event
from .fields import FIELD_KEYS, name_key, normalize
from .models import Account, Certificate, ReminderSent, Renewal, Vendor

PLACEHOLDER_PREFIX = "Unreadable certificate"
STUCK_AFTER = timedelta(minutes=10)


class UploadRejected(Exception):
    pass


def store_upload(session: Session, account: Account, filename: str, data: bytes, source: str,
                 vendor: Optional[Vendor] = None) -> Certificate:
    s = get_settings()
    mt = extraction.media_type_for(filename)
    if not mt:
        raise UploadRejected(f"{filename}: only PDF, JPG, PNG or WEBP files")
    if len(data) > s.max_upload_mb * 1024 * 1024:
        raise UploadRejected(f"{filename}: larger than {s.max_upload_mb} MB")
    if mt == extraction.PDF and not data.startswith(b"%PDF"):
        raise UploadRejected(f"{filename}: not a PDF file")
    digest = hashlib.sha256(data).hexdigest()
    dup = session.scalar(select(Certificate).where(Certificate.account_id == account.id, Certificate.sha256 == digest))
    if dup is not None:
        raise UploadRejected(f"{filename}: already uploaded")
    folder = os.path.join(s.upload_dir, str(account.id))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{uuid.uuid4().hex}{os.path.splitext(filename)[1].lower()}")
    with open(path, "wb") as fh:
        fh.write(data)
    cert = Certificate(account_id=account.id, vendor_id=vendor.id if vendor else None, source=source,
                       original_filename=os.path.basename(filename)[:300], stored_path=path, media_type=mt,
                       sha256=digest, extraction_status="queued")
    session.add(cert)
    session.flush()
    log_event(session, "certificate_uploaded", account.id, certificate_id=cert.id, source=source,
              vendor_id=cert.vendor_id, filename=cert.original_filename)
    return cert


def _find_or_create_vendor(session: Session, account: Account, insured: str, fallback: str) -> Vendor:
    key = name_key(insured)
    if key:
        v = session.scalar(select(Vendor).where(Vendor.account_id == account.id, Vendor.name_key == key,
                                                Vendor.active.is_(True)))
        if v is not None:
            return v
        name = insured
    else:
        name = f"{PLACEHOLDER_PREFIX} ({fallback})"
        key = name_key(name)
    v = Vendor(account_id=account.id, name=name[:200], name_key=key[:200])
    session.add(v)
    session.flush()
    log_event(session, "vendor_created", account.id, vendor_id=v.id, name=v.name, auto=True)
    return v


def _names_match(a: str, b: str) -> bool:
    ka, kb = name_key(a), name_key(b)
    if not ka or not kb:
        return False
    return ka == kb or ka in kb or kb in ka or difflib.SequenceMatcher(None, ka, kb).ratio() >= 0.75


def review_state_for(fields: dict, vendor: Optional[Vendor]) -> tuple[str, list[str]]:
    thr = get_settings().review_confidence
    low = [k for k in FIELD_KEYS if (fields.get(k) or {}).get("confidence", 0) < thr
           and ((fields.get(k) or {}).get("value") or _matters_when_empty(k, fields))]
    return ("needs_review" if low else "auto_accepted"), low


def _matters_when_empty(key: str, fields: dict) -> bool:
    """An empty low-confidence field matters if it belongs to a policy that IS on the certificate (or is a core
    top field). An empty field for a policy type that simply isn't listed is fine."""
    if "." not in key:
        return key in ("insured_name",)
    t = key.split(".")[0]
    return bool((fields.get(f"{t}.policy_number") or {}).get("value") or
                (fields.get(f"{t}.expiration_date") or {}).get("value"))


def after_certificate_change(session: Session, account: Account, vendor: Vendor, cert: Certificate,
                             old_next, now: Optional[datetime] = None) -> None:
    status.recompute_vendor(session, account, vendor, now)
    new_next = vendor.next_expiration
    if old_next and new_next and new_next > old_next and cert.extraction_status == "done":
        exists = session.scalar(select(Renewal).where(Renewal.vendor_id == vendor.id,
                                                      Renewal.previous_expiration == old_next))
        if exists is None:
            first = session.scalar(select(ReminderSent.sent_at).where(
                ReminderSent.vendor_id == vendor.id, ReminderSent.expiration_date == old_next)
                .order_by(ReminderSent.sent_at.asc()).limit(1))
            received = now or utcnow()
            hours = round((received - first).total_seconds() / 3600, 1) if first else None
            session.add(Renewal(account_id=account.id, vendor_id=vendor.id, certificate_id=cert.id, source=cert.source,
                                previous_expiration=old_next, new_expiration=new_next, first_reminder_at=first,
                                hours_from_first_reminder=hours, received_at=received))
            log_event(session, "renewal_received", account.id, vendor_id=vendor.id, certificate_id=cert.id,
                      source=cert.source, previous_expiration=old_next.isoformat(),
                      new_expiration=new_next.isoformat(), hours_from_first_reminder=hours)


def process_certificate(cert_id: int, now: Optional[datetime] = None) -> str:
    """Read one certificate and update everything that depends on it. Safe to call twice."""
    with session_scope() as session:
        cert = session.get(Certificate, cert_id)
        if cert is None or cert.extraction_status != "queued":   # claimed by another run already
            return cert.extraction_status if cert else "missing"
        cert.extraction_status = "processing"
        account_id = cert.account_id
    try:
        data = open(cert.stored_path, "rb").read()
        result = extraction.extract(data, cert.media_type)
    except Exception as exc:
        result = extraction.ExtractionResult(fields=extraction.empty_fields(), extractor="error",
                                             error=f"{exc.__class__.__name__}: {exc}"[:500])
    with session_scope() as session:
        cert = session.get(Certificate, cert_id)
        account = session.get(Account, account_id)
        cert.extracted = result.fields
        cert.extractor = result.extractor
        cert.extract_ms = result.ms
        cert.extraction_error = result.error
        cert.extraction_status = "done"
        insured = (result.fields.get("insured_name") or {}).get("value", "")
        vendor = session.get(Vendor, cert.vendor_id) if cert.vendor_id else None
        if vendor is None:
            vendor = _find_or_create_vendor(session, account, insured, cert.original_filename)
            cert.vendor_id = vendor.id
        old_next = vendor.next_expiration
        state, low = review_state_for(result.fields, vendor)
        # a sub's link upload whose insured name doesn't look like the vendor -> a human checks it
        if insured and not vendor.name.startswith(PLACEHOLDER_PREFIX) and not _names_match(insured, vendor.name):
            result.fields["insured_name"]["confidence"] = min(result.fields["insured_name"]["confidence"], 0.4)
            cert.extracted = dict(result.fields)
            if "insured_name" not in low:
                low = ["insured_name"] + low
            state = "needs_review"
        if result.error and not any((result.fields.get(k) or {}).get("value") for k in FIELD_KEYS):
            state = "needs_review"
            low = list(FIELD_KEYS)      # nothing was read: every field is for a human
        cert.review_state = state
        cert.low_confidence_fields = low
        # fill the vendor's agent contact from the certificate when the customer hasn't typed one
        for attr, key in (("agent_name", "producer_name"), ("agent_email", "producer_email"),
                          ("agent_phone", "producer_phone")):
            val = (result.fields.get(key) or {}).get("value", "")
            conf = (result.fields.get(key) or {}).get("confidence", 0)
            if val and not getattr(vendor, attr) and conf >= get_settings().review_confidence:
                setattr(vendor, attr, val[:200])
        log_event(session, "extraction_done", account.id, certificate_id=cert.id, vendor_id=vendor.id,
                  extractor=cert.extractor, review_state=state, low_confidence=len(low), ms=result.ms,
                  error=result.error, notes=result.notes)
        after_certificate_change(session, account, vendor, cert, old_next, now)
        return state


def apply_review(session: Session, account: Account, cert: Certificate, submitted: dict, now=None) -> int:
    """The customer confirmed/corrected the fields. Logs every change (accuracy measure). Returns #corrections."""
    from .models import FieldCorrection, ReviewLog

    extracted = cert.extracted or {}
    confirmed, corrections = {}, 0
    low = set(cert.low_confidence_fields or [])
    low_corrected = 0
    total = 0
    for k in FIELD_KEYS:
        old = (extracted.get(k) or {}).get("value", "")
        new = normalize(k, submitted.get(k, old))
        confirmed[k] = new
        if old or new:
            total += 1
        if new != old:
            corrections += 1
            low_corrected += k in low
            session.add(FieldCorrection(account_id=account.id, certificate_id=cert.id, field=k, extracted_value=old,
                                        corrected_value=new, confidence=(extracted.get(k) or {}).get("confidence", 0),
                                        extractor=cert.extractor))
    session.add(ReviewLog(account_id=account.id, certificate_id=cert.id, extractor=cert.extractor, fields_total=total,
                          fields_corrected=corrections, low_conf_total=len(low), low_conf_corrected=low_corrected))
    cert.confirmed = confirmed
    cert.review_state = "confirmed"
    cert.reviewed_at = utcnow()
    vendor = session.get(Vendor, cert.vendor_id)
    old_next = vendor.next_expiration if vendor else None
    # placeholder vendor (scan nobody could read) -> rename, or move the certificate to the matching vendor
    insured = confirmed.get("insured_name", "")
    if vendor is not None and vendor.name.startswith(PLACEHOLDER_PREFIX) and insured:
        match = session.scalar(select(Vendor).where(Vendor.account_id == account.id,
                                                    Vendor.name_key == name_key(insured), Vendor.id != vendor.id,
                                                    Vendor.active.is_(True)))
        if match is not None:
            vendor.active = False
            cert.vendor_id = match.id
            vendor, old_next = match, match.next_expiration
        else:
            vendor.name, vendor.name_key = insured[:200], name_key(insured)[:200]
    if vendor is not None:
        for attr, key in (("agent_name", "producer_name"), ("agent_email", "producer_email"),
                          ("agent_phone", "producer_phone")):
            if confirmed.get(key) and not getattr(vendor, attr):
                setattr(vendor, attr, confirmed[key][:200])
    log_event(session, "extraction_reviewed", account.id, certificate_id=cert.id, corrections=corrections,
              fields_total=total, extractor=cert.extractor)
    session.flush()
    if vendor is not None:
        after_certificate_change(session, account, vendor, cert, old_next, now)
    return corrections


def retry_stuck(now: Optional[datetime] = None) -> int:
    now = now or utcnow()
    with session_scope() as session:
        # queued for over a minute (the upload's own background task never ran, e.g. a restart) or
        # "processing" for over STUCK_AFTER (the process died mid-read)
        ids = [c.id for c in session.scalars(select(Certificate).where(
            Certificate.extraction_status.in_(("queued", "processing")))).all()
            if (c.extraction_status == "queued" and now - c.created_at > timedelta(minutes=1))
            or (now - c.created_at) > STUCK_AFTER]
        for cid in ids:
            c = session.get(Certificate, cid)
            c.extraction_status = "queued"
    for cid in ids:
        process_certificate(cid, now)
    return len(ids)
