"""Automatic renewal requests (30 / 14 / 7 / 0 days) and the weekly digest.

Renewal requests: for every vendor with a tracked expiration date, the due stage is the smallest stage that is
>= days left (12 days left -> the 14-day email). Each (vendor, expiration date, stage) is sent once
(unique constraint), so a run that repeats, or a worker that runs every 15 minutes, never double-sends. A stage
that was missed (e.g. the certificate was uploaded with 5 days left) is skipped; only the current one goes out.
After the expiration-day email nothing more is sent automatically for that date (v2: optional follow-ups).
A new certificate with a later expiration date simply moves the vendor to a new date: old stages stop by themselves.

The email goes to the sub (vendor email, if the customer added one) AND the agent (vendor agent email, filled
automatically from the certificate). Reply-To = the customer. Paid plans only.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import billing, messaging
from .config import get_settings
from .db import utcnow
from .eventlog import log_event
from .models import Account, Certificate, EventLog, ReminderSent, Vendor
from .status import STATUS_LABELS, local_today, requirements_for

NOT_ADVICE = ("This is an automated tracking reminder. It is not insurance advice and does not say whether "
              "coverage is adequate.")


def due_stage(days_left: int, stages: list[int]) -> Optional[int]:
    eligible = [s for s in stages if days_left <= s]
    return min(eligible) if eligible else None


def upload_link(vendor: Vendor) -> str:
    return f"{get_settings().base_url.rstrip('/')}/u/{vendor.upload_token}"


def recipients(vendor: Vendor) -> list[str]:
    out = []
    for a in (vendor.email, vendor.agent_email):
        a = (a or "").strip().lower()
        if a and "@" in a and a not in out:
            out.append(a)
    return out


def requirement_lines(account: Account) -> list[str]:
    r = requirements_for(account)
    lines = []
    if r.get("gl_each_occurrence") or r.get("gl_general_aggregate"):
        lines.append(f"General liability: ${int(r.get('gl_each_occurrence') or 0):,} each occurrence / "
                     f"${int(r.get('gl_general_aggregate') or 0):,} aggregate")
    if r.get("auto_combined_single_limit"):
        lines.append(f"Auto liability: ${int(r['auto_combined_single_limit']):,} combined single limit")
    if r.get("wc_required"):
        lines.append("Workers compensation on file")
    if r.get("umbrella_each_occurrence"):
        lines.append(f"Umbrella / excess: ${int(r['umbrella_each_occurrence']):,} each occurrence")
    if r.get("additional_insured_required"):
        lines.append(f"{account.name} listed as additional insured")
    if r.get("holder_name_contains"):
        lines.append(f"Certificate holder: {r['holder_name_contains']}")
    return lines


def renewal_email(account: Account, vendor: Vendor, days_left: int, exp) -> tuple[str, str]:
    if days_left > 0:
        when = f"expires on {exp:%B %d, %Y} ({days_left} days from today)"
        subject = f"Updated certificate of insurance needed - {vendor.name} ({days_left} days)"
    elif days_left == 0:
        when = f"expires today, {exp:%B %d, %Y}"
        subject = f"Certificate of insurance expires today - {vendor.name}"
    else:
        when = f"expired on {exp:%B %d, %Y}"
        subject = f"Certificate of insurance expired - {vendor.name}"
    s = get_settings()
    reqs = requirement_lines(account)
    body = "\n".join([
        "Hello,",
        "",
        f"{account.name} has a certificate of insurance on file for {vendor.name} that {when}.",
        "",
        "Please upload the renewed certificate here (no login needed):",
        upload_link(vendor),
        "",
        "PDF or a clear photo both work. Agents can upload directly with the same link.",
        *([""] + [f"{account.name} asks for:"] + [f"  - {x}" for x in reqs] if reqs else []),
        "",
        f"Questions? Reply to this email to reach {account.name}.",
        "",
        "--",
        NOT_ADVICE,
        *([s.mailing_address] if s.mailing_address else []),
    ])
    return subject, body


def send_renewal_request(session: Session, account: Account, vendor: Vendor, days_left: int, exp,
                         stage: Optional[int], test: bool = False) -> dict:
    s = get_settings()
    to = recipients(vendor)
    if not to:
        log_event(session, "reminder_no_recipient", account.id, vendor_id=vendor.id, stage=stage, test=test)
        return {"sent": 0, "reason": "no email for the sub or the agent"}
    subject, body = renewal_email(account, vendor, days_left, exp)
    if test:
        subject = "[TEST] " + subject
    statuses = []
    for addr in to:
        r = messaging.send_and_record(session, account.id, "renewal_test" if test else "renewal_request", addr,
                                      subject, body, from_name=account.name or s.app_name,
                                      reply_to=account.reply_email)
        statuses.append(r.status)
    log_event(session, "reminder_test_sent" if test else "reminder_sent", account.id, vendor_id=vendor.id,
              stage=stage, days_left=days_left, expiration=exp.isoformat(), to=to, statuses=statuses)
    return {"sent": len(to), "statuses": statuses, "to": to}


def process_account(session: Session, account: Account, now: Optional[datetime] = None) -> dict:
    now = now or utcnow()
    today = local_today(account, now)
    stages = get_settings().stages()
    out = {"sent": 0, "skipped_plan": 0, "no_recipient": 0}
    if not account.reminders_enabled:
        return out
    vendors = session.scalars(select(Vendor).where(Vendor.account_id == account.id, Vendor.active.is_(True),
                                                   Vendor.next_expiration.is_not(None))).all()
    for v in vendors:
        if v.status == "needs_review":
            continue     # don't email off numbers nobody has confirmed
        days_left = (v.next_expiration - today).days
        stage = due_stage(days_left, stages)
        if stage is None:
            continue
        if session.scalar(select(ReminderSent).where(ReminderSent.vendor_id == v.id,
                                                     ReminderSent.expiration_date == v.next_expiration,
                                                     ReminderSent.stage <= stage)):
            continue     # this stage (or a later one) already went out for this date
        if not billing.is_paid(account):
            if not session.scalar(select(EventLog.id).where(
                    EventLog.account_id == account.id, EventLog.type == "reminder_skipped_free_plan",
                    EventLog.created_at >= now - timedelta(days=1))):
                log_event(session, "reminder_skipped_free_plan", account.id, vendor_id=v.id, stage=stage)
            out["skipped_plan"] += 1
            continue
        res = send_renewal_request(session, account, v, days_left, v.next_expiration, stage)
        row = ReminderSent(account_id=account.id, vendor_id=v.id, expiration_date=v.next_expiration, stage=stage,
                           recipients=res.get("to", []),
                           delivery_status=(res.get("statuses") or ["skipped_no_recipient"])[0],
                           error=res.get("reason", ""), sent_at=now)
        try:
            with session.begin_nested():
                session.add(row)
        except IntegrityError:
            continue
        if res["sent"]:
            out["sent"] += 1
        else:
            out["no_recipient"] += 1
    return out


def pending_free_reminders(session: Session, account: Account, now: Optional[datetime] = None) -> int:
    """How many vendors would get a renewal request right now if the account were on a paid plan."""
    today = local_today(account, now)
    stages = get_settings().stages()
    n = 0
    for v in session.scalars(select(Vendor).where(Vendor.account_id == account.id, Vendor.active.is_(True),
                                                  Vendor.next_expiration.is_not(None))).all():
        if v.status != "needs_review" and due_stage((v.next_expiration - today).days, stages) is not None:
            n += 1
    return n


# --- weekly digest ----------------------------------------------------------------------------------------------
def digest_due(session: Session, account: Account, now: Optional[datetime] = None) -> Optional[str]:
    s = get_settings()
    now = now or utcnow()
    if not account.digest_enabled or not account.reply_email:
        return None
    if not billing.is_paid(account) and not s.digest_on_free_plan:
        return None
    from zoneinfo import ZoneInfo

    local = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(account.timezone or "America/New_York"))
    if local.weekday() != s.digest_weekday or local.hour < s.digest_hour:
        return None
    week = f"{local.isocalendar()[0]}-W{local.isocalendar()[1]:02d}"
    for e in session.scalars(select(EventLog).where(EventLog.account_id == account.id,
                                                    EventLog.type == "digest_sent")).all():
        if (e.detail or {}).get("week") == week:
            return None
    return week


def build_digest(session: Session, account: Account, now: Optional[datetime] = None) -> tuple[str, str]:
    now = now or utcnow()
    today = local_today(account, now)
    vendors = session.scalars(select(Vendor).where(Vendor.account_id == account.id, Vendor.active.is_(True))).all()
    expiring = sorted([v for v in vendors if v.status == "expiring"], key=lambda v: v.next_expiration or today)
    expired = sorted([v for v in vendors if v.status == "expired"], key=lambda v: v.next_expiration or today)
    review = [v for v in vendors if v.status == "needs_review"]
    missing = [v for v in vendors if v.status == "missing"]
    names = {v.id: v.name for v in vendors}
    came_in = session.scalars(select(Certificate).where(Certificate.account_id == account.id,
                                                        Certificate.created_at >= now - timedelta(days=7))
                              .order_by(Certificate.created_at.desc())).all()
    base = get_settings().base_url.rstrip("/")

    def line(v):
        d = f"{v.next_expiration:%b %d}" if v.next_expiration else "-"
        return f"  - {v.name}: {d} ({'; '.join((v.status_reasons or [])[:2])})"

    sections = [f"Weekly certificate summary for {account.name} (week of {today:%B %d, %Y})", ""]
    counts = {k: sum(1 for v in vendors if v.status == k) for k in STATUS_LABELS}
    sections.append(" | ".join(f"{STATUS_LABELS[k]}: {counts[k]}" for k in STATUS_LABELS))
    sections += ["", f"EXPIRING IN 30 DAYS ({len(expiring)})"] + ([line(v) for v in expiring] or ["  none"])
    sections += ["", f"EXPIRED ({len(expired)})"] + ([line(v) for v in expired] or ["  none"])
    sections += ["", f"CAME IN THIS WEEK ({len(came_in)})"] + (
        [f"  - {names.get(c.vendor_id, c.original_filename)} ({'sub link' if c.source == 'sub_link' else 'you uploaded'}"
         f", {c.created_at:%b %d})" for c in came_in] or ["  none"])
    if missing:
        sections += ["", f"MISSING A REQUIREMENT ({len(missing)})"] + [line(v) for v in missing]
    if review:
        sections += ["", f"NEEDS A QUICK CHECK ({len(review)}): {base}/review"]
    if not billing.is_paid(account):
        sections += ["", "Renewal requests are off on the free scan. Turn them on: " + base + "/billing"]
    sections += ["", f"Dashboard: {base}/dashboard", "", "--", NOT_ADVICE]
    subject = (f"{account.name}: {len(expiring)} expiring, {len(expired)} expired, {len(came_in)} new this week")
    return subject, "\n".join(sections)


def maybe_send_digest(session: Session, account: Account, now: Optional[datetime] = None, force: bool = False) -> bool:
    week = digest_due(session, account, now)
    if week is None and not force:
        return False
    subject, body = build_digest(session, account, now)
    r = messaging.send_and_record(session, account.id, "digest", account.reply_email, subject, body,
                                  from_name=get_settings().app_name)
    log_event(session, "digest_sent", account.id, week=week or "manual", status=r.status)
    return True
