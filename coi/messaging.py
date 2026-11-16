"""Email delivery, adapted from the Recovery Engine's app/messaging.py + app/allowlist.py.

DRY_RUN=true (default): nothing leaves the machine; each email is written to OUTBOX_DIR as a text file and to
the outbound_emails table (status "dry_run"). Live: SMTP. EMAIL_ALLOWLIST limits live sends to listed addresses.
"""
from __future__ import annotations

import os
import re
import smtplib
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Optional

from .config import get_settings
from .models import OutboundEmail

HELD_EMAIL = "not on EMAIL_ALLOWLIST"


@dataclass
class SendResult:
    ok: bool
    status: str             # sent | dry_run | failed
    provider_id: str = ""
    error: str = ""


def _write_outbox(header: str, body: str) -> str:
    s = get_settings()
    os.makedirs(s.outbox_dir, exist_ok=True)
    msg_id = uuid.uuid4().hex[:12]
    with open(os.path.join(s.outbox_dir, f"email_{msg_id}.txt"), "w", encoding="utf-8") as fh:
        fh.write(header + "\n\n" + body + "\n")
    return f"dryrun-{msg_id}"


def _address(addr: str) -> str:
    m = re.search(r"<([^>]+)>", addr or "")
    return (m.group(1) if m else (addr or "")).strip().lower()


def email_held(addr: str) -> bool:
    s = get_settings()
    entries = [e.strip().lower() for e in (s.email_allowlist or "").split(",") if e.strip()]
    if s.dry_run or not entries:
        return False
    a = _address(addr)
    return not any((a.endswith(e) if e.startswith("@") else a == e) for e in entries)


def send_email(to_addr: str, subject: str, body: str, from_name: str, reply_to: str = "",
               from_address: Optional[str] = None) -> SendResult:
    s = get_settings()
    from_address = from_address or s.email_from_address
    header = f"FROM: {from_name} <{from_address}>\nREPLY-TO: {reply_to}\nTO: {to_addr}\nSUBJECT: {subject}"
    if s.dry_run or s.email_mode == "dry_run":
        return SendResult(ok=True, status="dry_run", provider_id=_write_outbox(header, body))
    if email_held(to_addr):
        return SendResult(ok=True, status="dry_run", provider_id=_write_outbox(header + f"\nHELD: {HELD_EMAIL}", body),
                          error=HELD_EMAIL)
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_address))
    msg["To"] = to_addr
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    message_id = make_msgid(domain=from_address.split("@")[-1])
    msg["Message-ID"] = message_id
    msg.set_content(body)
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
            if s.smtp_use_tls:
                smtp.starttls()
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)
        return SendResult(ok=True, status="sent", provider_id=message_id)
    except Exception as exc:
        return SendResult(ok=False, status="failed", error=f"{exc.__class__.__name__}: {exc}")


def send_and_record(session, account_id: Optional[int], kind: str, to_addr: str, subject: str, body: str,
                    from_name: str, reply_to: str = "") -> SendResult:
    r = send_email(to_addr, subject, body, from_name=from_name, reply_to=reply_to)
    session.add(OutboundEmail(account_id=account_id, kind=kind, to_addr=to_addr, subject=subject, body=body,
                              status=r.status, provider_id=r.provider_id, error=r.error))
    return r
