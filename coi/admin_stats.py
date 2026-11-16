"""Numbers for the operator's /admin page: extraction accuracy over time, renewal rate, time to renewal, customers."""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import utcnow
from .models import Account, Certificate, EventLog, FieldCorrection, ReminderSent, Renewal, ReviewLog


def accuracy_by_week(db: Session) -> list[dict]:
    weeks = defaultdict(lambda: {"reviews": 0, "fields": 0, "corrected": 0, "low": 0, "low_corrected": 0})
    for r in db.scalars(select(ReviewLog)).all():
        y, w, _ = r.created_at.isocalendar()
        k = (f"{y}-W{w:02d}", r.extractor or "?")
        weeks[k]["reviews"] += 1
        weeks[k]["fields"] += r.fields_total
        weeks[k]["corrected"] += r.fields_corrected
        weeks[k]["low"] += r.low_conf_total
        weeks[k]["low_corrected"] += r.low_conf_corrected
    out = []
    for (week, extractor), v in sorted(weeks.items(), reverse=True):
        acc = 1 - v["corrected"] / v["fields"] if v["fields"] else None
        hi_fields = v["fields"] - v["low"]
        hi_wrong = v["corrected"] - v["low_corrected"]
        out.append(dict(week=week, extractor=extractor, reviews=v["reviews"], fields=v["fields"],
                        corrected=v["corrected"], accuracy=acc,
                        high_conf_accuracy=(1 - hi_wrong / hi_fields) if hi_fields > 0 else None))
    return out


def most_corrected_fields(db: Session, limit: int = 10) -> list[tuple[str, int]]:
    rows = db.execute(select(FieldCorrection.field, func.count(FieldCorrection.id)).group_by(FieldCorrection.field)
                      .order_by(func.count(FieldCorrection.id).desc()).limit(limit)).all()
    return [(r[0], r[1]) for r in rows]


def renewal_stats(db: Session) -> dict:
    reminded = db.execute(select(ReminderSent.vendor_id, ReminderSent.expiration_date)
                          .where(ReminderSent.delivery_status.in_(("sent", "dry_run")))
                          .group_by(ReminderSent.vendor_id, ReminderSent.expiration_date)).all()
    renewals = db.scalars(select(Renewal)).all()
    renewed_keys = {(r.vendor_id, r.previous_expiration) for r in renewals}
    after_reminder = [r for r in renewals if r.first_reminder_at is not None]
    hours = [r.hours_from_first_reminder for r in after_reminder if r.hours_from_first_reminder is not None]
    n_rem = len(reminded)
    n_ren = sum(1 for k in reminded if (k[0], k[1]) in renewed_keys)
    return dict(reminded=n_rem, renewed_after_reminder=n_ren, rate=(n_ren / n_rem) if n_rem else None,
                renewals_total=len(renewals), via_link=sum(1 for r in renewals if r.source == "sub_link"),
                median_hours=median(hours) if hours else None,
                avg_hours=(sum(hours) / len(hours)) if hours else None,
                reminders_sent=db.scalar(select(func.count(ReminderSent.id))) or 0)


def customer_stats(db: Session) -> dict:
    now = utcnow()
    accounts = db.scalars(select(Account)).all()
    recent = {r[0] for r in db.execute(select(Certificate.account_id)
                                       .where(Certificate.created_at >= now - timedelta(days=30))).all()}
    return dict(total=len(accounts), paid=sum(1 for a in accounts if a.plan_status == "active"),
                free=sum(1 for a in accounts if a.plan_status == "free"),
                active_30d=len(recent),
                signups_7d=sum(1 for a in accounts if a.created_at >= now - timedelta(days=7)))


def extraction_volume(db: Session) -> dict:
    certs = db.scalars(select(Certificate)).all()
    return dict(total=len(certs), auto_accepted=sum(1 for c in certs if c.review_state == "auto_accepted"),
                needs_review=sum(1 for c in certs if c.review_state == "needs_review"),
                confirmed=sum(1 for c in certs if c.review_state == "confirmed"),
                failed=sum(1 for c in certs if c.extraction_error),
                by_extractor=dict(db.execute(select(Certificate.extractor, func.count(Certificate.id))
                                             .group_by(Certificate.extractor)).all()))


def recent_events(db: Session, limit: int = 40) -> list[EventLog]:
    return db.scalars(select(EventLog).order_by(EventLog.id.desc()).limit(limit)).all()
