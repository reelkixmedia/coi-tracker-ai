"""Web app (FastAPI + Jinja2, server-rendered, same style as the Recovery Engine's app/web/main.py:
SessionMiddleware login, CSRF token per session, flash messages, get_db dependency).

Run:  .venv\\Scripts\\python.exe -m uvicorn coi.web.main:app --port 8010
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .. import admin_stats, billing, processing, reminders, status, worker
from ..config import get_settings
from ..db import SessionLocal, init_db, utcnow
from ..eventlog import log_event
from ..fields import FIELDS, POLICY_TYPES, as_int, name_key
from ..models import (DEFAULT_REQUIREMENTS, Account, Certificate, OutboundEmail, ReminderSent, Renewal, User,
                      Vendor)
from ..security import hash_password, verify_password

TEMPLATES = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
TIMEZONES = ["America/New_York", "America/Chicago", "America/Denver", "America/Phoenix", "America/Los_Angeles",
             "America/Anchorage", "Pacific/Honolulu"]


def _linkify(text: str) -> Markup:
    esc = str(escape(text or ""))
    return Markup(re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', esc))


TEMPLATES.env.filters["linkify"] = _linkify
TEMPLATES.env.filters["money"] = lambda v: f"${as_int(v):,}" if as_int(v) else ""


def create_app() -> FastAPI:
    s = get_settings()

    @asynccontextmanager
    async def lifespan(_app):
        init_db()
        if s.auto_scheduler:
            worker.start_background_loop()
        yield

    app = FastAPI(title=s.app_name, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=s.secret_key, same_site="lax",
                       https_only=s.base_url.startswith("https"), max_age=60 * 60 * 24 * 14)
    register_routes(app)
    return app


# --- helpers (same pattern as the Recovery Engine) ----------------------------------------------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


def check_csrf(request: Request, token: str) -> None:
    if not token or not secrets.compare_digest(token, request.session.get("csrf", "")):
        raise HTTPException(status_code=400, detail="Form expired - go back, refresh the page and try again.")


class LoginRequired(Exception):
    pass


def session_user(request: Request, db: Session) -> Optional[User]:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if user is not None and (user.session_gen or 0) != request.session.get("sv", 0):
        request.session.clear()
        return None
    return user


def log_in(request: Request, user: User) -> None:
    request.session.clear()
    request.session["uid"] = user.id
    request.session["sv"] = user.session_gen or 0
    admins = {e.strip().lower() for e in get_settings().admin_emails.split(",") if e.strip()}
    request.session["adm"] = (user.email or "").lower() in admins


def current_account(request: Request, db: Session = Depends(get_db)) -> Account:
    user = session_user(request, db)
    if user is None:
        raise LoginRequired()
    request.state.user = user
    return db.get(Account, user.account_id)


def flash(request: Request, msg: str) -> None:
    request.session["flash"] = msg


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    s = get_settings()
    ctx.update(request=request, csrf=csrf_token(request), app_name=s.app_name, dry_run=s.dry_run,
               flash=request.session.pop("flash", None), logged_in=bool(request.session.get("uid")),
               is_admin=bool(request.session.get("adm")), labels=status.STATUS_LABELS,
               base_url=s.base_url.rstrip("/"))
    return TEMPLATES.TemplateResponse(request, name, ctx)


def _own(db: Session, model, obj_id: int, account: Account):
    obj = db.get(model, obj_id)
    if obj is None or obj.account_id != account.id:
        raise HTTPException(status_code=404)
    return obj


def _process_all(ids: list[int]) -> None:
    for cid in ids:
        processing.process_certificate(cid)


_link_hits: dict[str, deque] = defaultdict(deque)


def _link_limited(token: str, per_hour: int = 20) -> bool:
    q = _link_hits[token]
    now = time.time()
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= per_hour:
        return True
    q.append(now)
    return False


def _sort_vendors(vendors: list[Vendor]) -> list[Vendor]:
    return sorted(vendors, key=lambda v: (v.next_expiration is None, v.next_expiration or 0, v.name.lower()))


# --- routes ---------------------------------------------------------------------------------------------------
def register_routes(app: FastAPI) -> None:
    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        return RedirectResponse("/login", status_code=303)

    @app.get("/")
    def home(request: Request):
        return RedirectResponse("/dashboard" if request.session.get("uid") else "/signup", status_code=303)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # signup / login ------------------------------------------------------------------------------------------
    @app.get("/signup", response_class=HTMLResponse)
    def signup_form(request: Request):
        return render(request, "signup.html", timezones=TIMEZONES)

    @app.post("/signup")
    def signup(request: Request, company: str = Form(...), email: str = Form(...), password: str = Form(...),
               timezone: str = Form("America/New_York"), csrf: str = Form(""), website: str = Form(""),
               db: Session = Depends(get_db)):
        check_csrf(request, csrf)
        if website:      # honeypot
            return RedirectResponse("/login", status_code=303)
        email = email.strip().lower()
        if len(password) < 8 or "@" not in email or not company.strip():
            flash(request, "Company name, a valid email and a password of 8+ characters, please.")
            return RedirectResponse("/signup", status_code=303)
        if db.scalar(select(User).where(User.email == email)):
            flash(request, "That email already has an account - log in instead.")
            return RedirectResponse("/login", status_code=303)
        acct = Account(name=company.strip()[:200], reply_email=email,
                       timezone=timezone if timezone in TIMEZONES else "America/New_York",
                       requirements=dict(DEFAULT_REQUIREMENTS))
        db.add(acct)
        db.flush()
        user = User(account_id=acct.id, email=email, password_hash=hash_password(password))
        db.add(user)
        db.flush()
        log_event(db, "signup", acct.id)
        log_in(request, user)
        return RedirectResponse("/setup?welcome=1", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return render(request, "login.html")

    @app.post("/login")
    def login(request: Request, email: str = Form(...), password: str = Form(...), csrf: str = Form(""),
              db: Session = Depends(get_db)):
        check_csrf(request, csrf)
        user = db.scalar(select(User).where(User.email == email.strip().lower()))
        if user is None or not verify_password(password, user.password_hash):
            flash(request, "Wrong email or password.")
            return RedirectResponse("/login", status_code=303)
        log_in(request, user)
        return RedirectResponse("/dashboard", status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # requirements / settings ------------------------------------------------------------------------------------
    @app.get("/setup", response_class=HTMLResponse)
    def setup_form(request: Request, welcome: int = 0, acct: Account = Depends(current_account)):
        return render(request, "setup.html", acct=acct, req=status.requirements_for(acct), welcome=welcome,
                      timezones=TIMEZONES)

    @app.post("/setup")
    def setup_save(request: Request, csrf: str = Form(""), gl_each_occurrence: str = Form("0"),
                   gl_general_aggregate: str = Form("0"), auto_combined_single_limit: str = Form("0"),
                   umbrella_each_occurrence: str = Form("0"), wc_required: str = Form(""),
                   additional_insured_required: str = Form(""), holder_name_contains: str = Form(""),
                   reply_email: str = Form(""), timezone: str = Form("America/New_York"),
                   reminders_enabled: str = Form(""), digest_enabled: str = Form(""),
                   acct: Account = Depends(current_account), db: Session = Depends(get_db)):
        check_csrf(request, csrf)
        from ..fields import norm_money

        acct.requirements = {
            "gl_each_occurrence": as_int(norm_money(gl_each_occurrence)),
            "gl_general_aggregate": as_int(norm_money(gl_general_aggregate)),
            "auto_combined_single_limit": as_int(norm_money(auto_combined_single_limit)),
            "umbrella_each_occurrence": as_int(norm_money(umbrella_each_occurrence)),
            "wc_required": bool(wc_required),
            "additional_insured_required": bool(additional_insured_required),
            "holder_name_contains": holder_name_contains.strip()[:200],
        }
        acct.requirements_set_at = utcnow()
        if reply_email.strip():
            acct.reply_email = reply_email.strip().lower()[:200]
        acct.timezone = timezone if timezone in TIMEZONES else acct.timezone
        acct.reminders_enabled = bool(reminders_enabled)
        acct.digest_enabled = bool(digest_enabled)
        log_event(db, "requirements_saved", acct.id, requirements=acct.requirements)
        db.flush()
        status.recompute_account(db, acct)
        flash(request, "Saved. Every vendor's status was rechecked against these requirements.")
        return RedirectResponse("/dashboard", status_code=303)

    # dashboard + upload ---------------------------------------------------------------------------------------
    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request, filter: str = "", acct: Account = Depends(current_account),
                  db: Session = Depends(get_db)):
        vendors = db.scalars(select(Vendor).where(Vendor.account_id == acct.id, Vendor.active.is_(True))).all()
        counts = {k: sum(1 for v in vendors if v.status == k) for k in status.STATUS_LABELS}
        shown = [v for v in vendors if not filter or v.status == filter]
        reading = db.scalars(select(Certificate).where(Certificate.account_id == acct.id,
                                                       Certificate.extraction_status.in_(("queued", "processing")))
                             ).all()
        today = status.local_today(acct)
        return render(request, "dashboard.html", acct=acct, counts=counts, vendors=_sort_vendors(shown),
                      filter=filter, reading=len(reading), today=today, paid=billing.is_paid(acct),
                      allowance=billing.upload_allowance(db, acct), can_bulk=billing.can_bulk(acct),
                      pending_free=0 if billing.is_paid(acct) else reminders.pending_free_reminders(db, acct),
                      requirements_set=bool(acct.requirements_set_at))

    @app.post("/upload")
    def upload(request: Request, background: BackgroundTasks, csrf: str = Form(""),
               files: List[UploadFile] = File(...), acct: Account = Depends(current_account),
               db: Session = Depends(get_db)):
        check_csrf(request, csrf)
        files = [f for f in files if f.filename]
        if not files:
            flash(request, "Pick at least one file.")
            return RedirectResponse("/dashboard", status_code=303)
        if len(files) > 1 and not billing.can_bulk(acct):
            flash(request, "Bulk upload is on paid plans. On the free scan, upload one certificate at a time.")
            return RedirectResponse("/dashboard", status_code=303)
        allowance = billing.upload_allowance(db, acct)
        if allowance is not None and len(files) > allowance:
            flash(request, f"The free scan covers {get_settings().free_scan_max_cois} certificates "
                           f"({allowance} left). Upgrade on the Billing page for more.")
            return RedirectResponse("/dashboard", status_code=303)
        ids, problems = [], []
        for f in files:
            try:
                cert = processing.store_upload(db, acct, f.filename, f.file.read(),
                                               source="bulk" if len(files) > 1 else "upload")
                ids.append(cert.id)
            except processing.UploadRejected as exc:
                problems.append(str(exc))
        db.commit()
        if ids:
            background.add_task(_process_all, ids)
        msg = f"Reading {len(ids)} certificate(s) now - statuses fill in by themselves."
        if problems:
            msg += " Skipped: " + "; ".join(problems[:5]) + (" ..." if len(problems) > 5 else "")
        flash(request, msg)
        return RedirectResponse("/dashboard", status_code=303)

    # vendors -------------------------------------------------------------------------------------------------
    @app.post("/vendors/new")
    def vendor_new(request: Request, csrf: str = Form(""), name: str = Form(...), email: str = Form(""),
                   agent_email: str = Form(""), acct: Account = Depends(current_account),
                   db: Session = Depends(get_db)):
        check_csrf(request, csrf)
        cap = billing.vendor_cap(acct)
        if cap and billing.vendor_count(db, acct) >= cap:
            flash(request, f"Your plan covers {cap} vendors.")
            return RedirectResponse("/dashboard", status_code=303)
        key = name_key(name)
        v = db.scalar(select(Vendor).where(Vendor.account_id == acct.id, Vendor.name_key == key,
                                           Vendor.active.is_(True)))
        if v is None:
            v = Vendor(account_id=acct.id, name=name.strip()[:200], name_key=key[:200])
            db.add(v)
        v.email = email.strip().lower()[:200] or v.email
        v.agent_email = agent_email.strip().lower()[:200] or v.agent_email
        db.flush()
        status.recompute_vendor(db, acct, v)
        log_event(db, "vendor_created", acct.id, vendor_id=v.id, name=v.name, auto=False)
        return RedirectResponse(f"/vendors/{v.id}", status_code=303)

    @app.get("/vendors/{vid}", response_class=HTMLResponse)
    def vendor_page(request: Request, vid: int, acct: Account = Depends(current_account),
                    db: Session = Depends(get_db)):
        v = _own(db, Vendor, vid, acct)
        certs = db.scalars(select(Certificate).where(Certificate.vendor_id == v.id)
                           .order_by(Certificate.created_at.desc())).all()
        sent = db.scalars(select(ReminderSent).where(ReminderSent.vendor_id == v.id)
                          .order_by(ReminderSent.sent_at.desc())).all()
        renewals = db.scalars(select(Renewal).where(Renewal.vendor_id == v.id)
                              .order_by(Renewal.received_at.desc())).all()
        cur = db.get(Certificate, v.current_certificate_id) if v.current_certificate_id else None
        vals = status.cert_values(cur) if cur else {}
        return render(request, "vendor.html", acct=acct, v=v, certs=certs, sent=sent, renewals=renewals,
                      link=reminders.upload_link(v), recipients=reminders.recipients(v), vals=vals,
                      policy_types=POLICY_TYPES, paid=billing.is_paid(acct))

    @app.post("/vendors/{vid}")
    def vendor_save(request: Request, vid: int, csrf: str = Form(""), name: str = Form(""), email: str = Form(""),
                    agent_name: str = Form(""), agent_email: str = Form(""), agent_phone: str = Form(""),
                    acct: Account = Depends(current_account), db: Session = Depends(get_db)):
        check_csrf(request, csrf)
        v = _own(db, Vendor, vid, acct)
        if name.strip():
            v.name, v.name_key = name.strip()[:200], name_key(name)[:200]
        v.email = email.strip().lower()[:200]
        v.agent_name = agent_name.strip()[:200]
        v.agent_email = agent_email.strip().lower()[:200]
        v.agent_phone = agent_phone.strip()[:60]
        log_event(db, "vendor_updated", acct.id, vendor_id=v.id)
        flash(request, "Saved.")
        return RedirectResponse(f"/vendors/{v.id}", status_code=303)

    @app.post("/vendors/{vid}/test-reminder")
    def vendor_test_reminder(request: Request, vid: int, csrf: str = Form(""),
                             acct: Account = Depends(current_account), db: Session = Depends(get_db)):
        check_csrf(request, csrf)
        v = _own(db, Vendor, vid, acct)
        today = status.local_today(acct)
        exp = v.next_expiration or today
        res = reminders.send_renewal_request(db, acct, v, (exp - today).days, exp, stage=None, test=True)
        if res["sent"]:
            where = "written to the outbox (dry run)" if get_settings().dry_run else "sent"
            flash(request, f"Test renewal request {where} for {', '.join(res['to'])}. Open Outbox to read it "
                           f"and click the upload link.")
        else:
            flash(request, "No email to send to: add the sub's email or the agent's email below.")
        return RedirectResponse(f"/vendors/{v.id}", status_code=303)

    @app.post("/vendors/{vid}/remove")
    def vendor_remove(request: Request, vid: int, csrf: str = Form(""), acct: Account = Depends(current_account),
                      db: Session = Depends(get_db)):
        check_csrf(request, csrf)
        v = _own(db, Vendor, vid, acct)
        v.active = False      # hidden, never deleted (history stays for tracking)
        log_event(db, "vendor_archived", acct.id, vendor_id=v.id)
        flash(request, f"{v.name} archived (no more reminders).")
        return RedirectResponse("/dashboard", status_code=303)

    # review -------------------------------------------------------------------------------------------------
    @app.get("/review", response_class=HTMLResponse)
    def review_queue(request: Request, acct: Account = Depends(current_account), db: Session = Depends(get_db)):
        certs = db.scalars(select(Certificate).where(Certificate.account_id == acct.id,
                                                     Certificate.review_state == "needs_review")
                           .order_by(Certificate.created_at.asc())).all()
        names = {v.id: v.name for v in db.scalars(select(Vendor).where(Vendor.account_id == acct.id)).all()}
        return render(request, "review_queue.html", acct=acct, certs=certs, names=names)

    @app.get("/certificates/{cid}", response_class=HTMLResponse)
    def cert_page(request: Request, cid: int, acct: Account = Depends(current_account),
                  db: Session = Depends(get_db)):
        c = _own(db, Certificate, cid, acct)
        v = db.get(Vendor, c.vendor_id) if c.vendor_id else None
        vals = status.cert_values(c)
        low = set(c.low_confidence_fields or [])
        thr = get_settings().review_confidence
        rows = []
        for key, label, kind, group in FIELDS:
            item = (c.extracted or {}).get(key) or {}
            rows.append(dict(key=key, label=label, kind=kind, group=group, value=vals.get(key, ""),
                             extracted=item.get("value", ""), conf=item.get("confidence", 0.0),
                             low=key in low or (item.get("value") and item.get("confidence", 0) < thr)))
        nxt = db.scalar(select(Certificate.id).where(Certificate.account_id == acct.id,
                                                     Certificate.review_state == "needs_review",
                                                     Certificate.id != c.id).order_by(Certificate.created_at.asc()))
        return render(request, "certificate.html", acct=acct, c=c, v=v, rows=rows, next_id=nxt,
                      low_count=sum(1 for r in rows if r["low"]))

    @app.post("/certificates/{cid}")
    async def cert_save(request: Request, cid: int, acct: Account = Depends(current_account),
                        db: Session = Depends(get_db)):
        form = await request.form()
        check_csrf(request, form.get("csrf", ""))
        c = _own(db, Certificate, cid, acct)
        submitted = {k: str(form.get(k, "")) for k, *_ in FIELDS}
        for k, _, kind, _ in FIELDS:     # unchecked checkbox = no
            if kind == "bool":
                submitted[k] = "yes" if form.get(k) else "no"
        n = processing.apply_review(db, acct, c, submitted)
        flash(request, f"Confirmed ({n} correction{'s' if n != 1 else ''} saved). Status updated.")
        go_next = form.get("next_id")
        return RedirectResponse(f"/certificates/{go_next}" if go_next else "/dashboard", status_code=303)

    @app.get("/certificates/{cid}/file")
    def cert_file(cid: int, acct: Account = Depends(current_account), db: Session = Depends(get_db)):
        c = _own(db, Certificate, cid, acct)
        if not os.path.exists(c.stored_path):
            raise HTTPException(status_code=404)
        return FileResponse(c.stored_path, media_type=c.media_type,
                            headers={"Content-Disposition": "inline", "X-Content-Type-Options": "nosniff"})

    # the sub's upload link (no login) -----------------------------------------------------------------------
    def _vendor_by_token(db: Session, token: str) -> Vendor:
        v = db.scalar(select(Vendor).where(Vendor.upload_token == token, Vendor.active.is_(True)))
        if v is None:
            raise HTTPException(status_code=404, detail="This upload link isn't active.")
        return v

    @app.get("/u/{token}", response_class=HTMLResponse)
    def sub_upload_form(request: Request, token: str, db: Session = Depends(get_db)):
        v = _vendor_by_token(db, token)
        acct = db.get(Account, v.account_id)
        return render(request, "sub_upload.html", v=v, acct=acct, reqs=reminders.requirement_lines(acct),
                      token=token, done=False)

    @app.post("/u/{token}", response_class=HTMLResponse)
    def sub_upload(request: Request, token: str, background: BackgroundTasks, csrf: str = Form(""),
                   files: List[UploadFile] = File(...), db: Session = Depends(get_db)):
        check_csrf(request, csrf)
        v = _vendor_by_token(db, token)
        acct = db.get(Account, v.account_id)
        if _link_limited(token):
            raise HTTPException(status_code=429, detail="Too many uploads from this link - try again in an hour.")
        ids, problems = [], []
        for f in [f for f in files if f.filename][:5]:
            try:
                ids.append(processing.store_upload(db, acct, f.filename, f.file.read(), source="sub_link",
                                                   vendor=v).id)
            except processing.UploadRejected as exc:
                problems.append(str(exc))
        db.commit()
        if ids:
            background.add_task(_process_all, ids)
        return render(request, "sub_upload.html", v=v, acct=acct, reqs=reminders.requirement_lines(acct),
                      token=token, done=bool(ids), problems=problems)

    # outbox (dry run: read what would have been emailed) ----------------------------------------------------
    @app.get("/outbox", response_class=HTMLResponse)
    def outbox(request: Request, acct: Account = Depends(current_account), db: Session = Depends(get_db)):
        mails = db.scalars(select(OutboundEmail).where(OutboundEmail.account_id == acct.id)
                           .order_by(OutboundEmail.id.desc()).limit(100)).all()
        return render(request, "outbox.html", acct=acct, mails=mails)

    @app.post("/digest/send-now")
    def digest_now(request: Request, csrf: str = Form(""), acct: Account = Depends(current_account),
                   db: Session = Depends(get_db)):
        check_csrf(request, csrf)
        status.recompute_account(db, acct)
        reminders.maybe_send_digest(db, acct, force=True)
        flash(request, "Weekly digest sent now (see Outbox).")
        return RedirectResponse("/outbox", status_code=303)

    # billing -------------------------------------------------------------------------------------------------
    @app.get("/billing", response_class=HTMLResponse)
    def billing_page(request: Request, acct: Account = Depends(current_account), db: Session = Depends(get_db)):
        return render(request, "billing.html", acct=acct, tiers=get_settings().plan_tiers(),
                      stripe_on=bool(get_settings().stripe_secret_key), dev=billing.dev_activation_allowed(),
                      used=billing.cert_count(db, acct), free_max=get_settings().free_scan_max_cois)

    @app.post("/billing/checkout")
    def billing_checkout(request: Request, csrf: str = Form(""), plan: str = Form(...),
                         acct: Account = Depends(current_account), db: Session = Depends(get_db)):
        check_csrf(request, csrf)
        url = billing.checkout_url(acct, request.state.user.email, plan)
        if not url:
            flash(request, "That plan isn't set up in Stripe yet (PLAN_TIERS_JSON stripe_price + STRIPE_SECRET_KEY).")
            return RedirectResponse("/billing", status_code=303)
        return RedirectResponse(url, status_code=303)

    @app.post("/billing/portal")
    def billing_portal(request: Request, csrf: str = Form(""), acct: Account = Depends(current_account)):
        check_csrf(request, csrf)
        url = billing.portal_url(acct)
        return RedirectResponse(url or "/billing", status_code=303)

    @app.post("/billing/dev-activate")
    def billing_dev(request: Request, csrf: str = Form(""), plan: str = Form("tier1"),
                    acct: Account = Depends(current_account), db: Session = Depends(get_db)):
        check_csrf(request, csrf)
        if not billing.dev_activation_allowed():
            raise HTTPException(status_code=403)
        acct.plan_status, acct.plan_key = "active", plan
        log_event(db, "dev_plan_activated", acct.id, plan=plan)
        flash(request, "Test plan active (local dry run only - no Stripe). Reminders and bulk upload are on.")
        return RedirectResponse("/dashboard", status_code=303)

    @app.post("/webhooks/stripe")
    async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
        s = get_settings()
        raw = await request.body()
        import stripe

        try:
            stripe.Webhook.construct_event(raw, request.headers.get("Stripe-Signature", ""), s.stripe_webhook_secret)
        except Exception:
            raise HTTPException(status_code=400, detail="bad signature")
        return {"result": billing.handle_event(db, json.loads(raw))}

    # admin (operator) -------------------------------------------------------------------------------------------
    @app.get("/admin", response_class=HTMLResponse)
    def admin(request: Request, acct: Account = Depends(current_account), db: Session = Depends(get_db)):
        if not request.session.get("adm"):
            raise HTTPException(status_code=404)
        return render(request, "admin.html", acct=acct, acc=admin_stats.accuracy_by_week(db),
                      fields=admin_stats.most_corrected_fields(db), ren=admin_stats.renewal_stats(db),
                      cust=admin_stats.customer_stats(db), vol=admin_stats.extraction_volume(db),
                      events=admin_stats.recent_events(db))

    @app.post("/admin/run-now")
    def admin_run(request: Request, csrf: str = Form(""), acct: Account = Depends(current_account)):
        check_csrf(request, csrf)
        if not request.session.get("adm"):
            raise HTTPException(status_code=404)
        worker.run_once()
        flash(request, "Scheduled pass ran now (statuses, due reminders, due digests).")
        return RedirectResponse("/admin", status_code=303)


app = create_app()
