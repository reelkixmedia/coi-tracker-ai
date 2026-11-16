"""Tables. One Account = one customer (a GC or property manager). Vendors = their subs/vendors."""
from __future__ import annotations

import secrets
from datetime import date, datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow


def _token() -> str:
    return secrets.token_urlsafe(24)


DEFAULT_REQUIREMENTS = {
    "gl_each_occurrence": 1_000_000,
    "gl_general_aggregate": 2_000_000,
    "auto_combined_single_limit": 0,      # 0 = not required
    "wc_required": True,
    "umbrella_each_occurrence": 0,
    "additional_insured_required": True,
    "holder_name_contains": "",           # "" = don't check the certificate holder
}


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    reply_email: Mapped[str] = mapped_column(String(200), default="")   # subs/agents reply here; digest goes here
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York")
    requirements: Mapped[Optional[dict]] = mapped_column(JSON, default=dict, nullable=True)
    requirements_set_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    plan_status: Mapped[str] = mapped_column(String(20), default="free")    # free | active | past_due | canceled
    plan_key: Mapped[str] = mapped_column(String(30), default="")
    stripe_customer_id: Mapped[str] = mapped_column(String(100), default="")
    stripe_subscription_id: Mapped[str] = mapped_column(String(100), default="")
    reminders_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    digest_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    session_gen: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Vendor(Base):
    __tablename__ = "vendors"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    name_key: Mapped[str] = mapped_column(String(200), index=True)      # normalised name for matching uploads
    email: Mapped[str] = mapped_column(String(200), default="")         # the sub's own email (customer adds it)
    agent_name: Mapped[str] = mapped_column(String(200), default="")
    agent_email: Mapped[str] = mapped_column(String(200), default="")
    agent_phone: Mapped[str] = mapped_column(String(60), default="")
    upload_token: Mapped[str] = mapped_column(String(64), default=_token, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="missing")  # compliant|expiring|expired|missing|needs_review
    status_reasons: Mapped[Optional[list]] = mapped_column(JSON, default=list, nullable=True)
    next_expiration: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    current_certificate_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status_computed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Certificate(Base):
    __tablename__ = "certificates"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    vendor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("vendors.id"), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(20), default="upload")   # upload | bulk | sub_link
    original_filename: Mapped[str] = mapped_column(String(300), default="")
    stored_path: Mapped[str] = mapped_column(String(500), default="")
    media_type: Mapped[str] = mapped_column(String(60), default="application/pdf")
    sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    extraction_status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|processing|done|failed
    extraction_error: Mapped[str] = mapped_column(Text, default="")
    extractor: Mapped[str] = mapped_column(String(40), default="")      # claude:<model> | local_text | none
    extracted: Mapped[Optional[dict]] = mapped_column(JSON, default=dict, nullable=True)   # field -> {value, confidence}
    confirmed: Mapped[Optional[dict]] = mapped_column(JSON, default=dict, nullable=True)   # field -> value (after review)
    review_state: Mapped[str] = mapped_column(String(20), default="pending")  # pending|needs_review|auto_accepted|confirmed
    low_confidence_fields: Mapped[Optional[list]] = mapped_column(JSON, default=list, nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    extract_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FieldCorrection(Base):
    """One row per field a human changed on review: the extraction accuracy measure."""
    __tablename__ = "field_corrections"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, index=True)
    certificate_id: Mapped[int] = mapped_column(ForeignKey("certificates.id"), index=True)
    field: Mapped[str] = mapped_column(String(60))
    extracted_value: Mapped[str] = mapped_column(Text, default="")
    corrected_value: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    extractor: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReviewLog(Base):
    """One row per human review: how many fields were looked at and how many were wrong."""
    __tablename__ = "review_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, index=True)
    certificate_id: Mapped[int] = mapped_column(ForeignKey("certificates.id"), index=True)
    extractor: Mapped[str] = mapped_column(String(40), default="")
    fields_total: Mapped[int] = mapped_column(Integer, default=0)
    fields_corrected: Mapped[int] = mapped_column(Integer, default=0)
    low_conf_total: Mapped[int] = mapped_column(Integer, default=0)
    low_conf_corrected: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReminderSent(Base):
    """A renewal request (30/14/7/0 days). Unique per vendor + expiration date + stage, so it never repeats."""
    __tablename__ = "reminders_sent"
    __table_args__ = (UniqueConstraint("vendor_id", "expiration_date", "stage", name="uq_reminder_stage"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, index=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), index=True)
    expiration_date: Mapped[date] = mapped_column(Date)
    stage: Mapped[int] = mapped_column(Integer)
    recipients: Mapped[Optional[list]] = mapped_column(JSON, default=list, nullable=True)
    delivery_status: Mapped[str] = mapped_column(String(20), default="")   # sent | dry_run | failed | skipped_*
    error: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Renewal(Base):
    """A newer certificate arrived for a vendor (pushed the next expiration later)."""
    __tablename__ = "renewals"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, index=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), index=True)
    certificate_id: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(20), default="")
    previous_expiration: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    new_expiration: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    first_reminder_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    hours_from_first_reminder: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OutboundEmail(Base):
    __tablename__ = "outbound_emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(30))      # renewal_request | renewal_test | digest | other
    to_addr: Mapped[str] = mapped_column(String(300))
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))
    provider_id: Mapped[str] = mapped_column(String(200), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EventLog(Base):
    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(60), index=True)
    detail: Mapped[Optional[dict]] = mapped_column(JSON, default=dict, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class StripeEventSeen(Base):
    __tablename__ = "stripe_events"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
