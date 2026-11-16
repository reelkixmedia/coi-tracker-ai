"""The scheduled pass (same shape as the Recovery Engine's app/worker.py). Each pass:
  1. reads any certificate still waiting (an upload whose background read never ran, e.g. after a restart)
  2. recomputes every vendor's status (dates move every day, so statuses change with no upload)
  3. sends renewal requests that are due (30/14/7/0 days; paid plans)
  4. sends the weekly digest when it's due (Monday 8 am the customer's time, once per week)

By default the web app runs this itself every WORKER_INTERVAL_SECONDS (AUTO_SCHEDULER=true), so starting the
app is the only step. To run it separately (cron / Render cron / GitHub Actions):

    .venv\\Scripts\\python.exe -m coi.worker --once
"""
from __future__ import annotations

import argparse
import threading
import time
import traceback
from datetime import datetime
from typing import Optional

from sqlalchemy import select

from . import processing, reminders, status
from .config import get_settings
from .db import init_db, session_scope, utcnow
from .eventlog import log_event
from .models import Account

_lock = threading.Lock()


def run_once(now: Optional[datetime] = None) -> dict:
    if not _lock.acquire(blocking=False):
        return {"skipped": "another pass is running"}
    try:
        now = now or utcnow()
        summary = {"retried_reads": processing.retry_stuck(now), "accounts": {}}
        with session_scope() as session:
            ids = [a.id for a in session.scalars(select(Account)).all()]
        for aid in ids:
            try:
                with session_scope() as session:
                    account = session.get(Account, aid)
                    counts = status.recompute_account(session, account, now)
                    session.flush()
                    sent = reminders.process_account(session, account, now)
                    digest = reminders.maybe_send_digest(session, account, now)
                    summary["accounts"][aid] = {"status": counts, "reminders": sent, "digest": digest}
            except Exception as exc:   # one broken account never stops the others
                with session_scope() as session:
                    log_event(session, "worker_error", aid, error=f"{exc.__class__.__name__}: {exc}",
                              trace=traceback.format_exc()[-1500:])
                summary["accounts"][aid] = {"error": str(exc)}
        with session_scope() as session:
            log_event(session, "worker_pass", None, accounts=len(ids), retried=summary["retried_reads"])
        return summary
    finally:
        _lock.release()


def start_background_loop() -> threading.Thread:
    """Used by the web app when AUTO_SCHEDULER=true."""
    interval = max(60, get_settings().worker_interval_seconds)

    def loop():
        time.sleep(5)
        while True:
            try:
                run_once()
            except Exception:
                traceback.print_exc()
            time.sleep(interval)

    t = threading.Thread(target=loop, name="coi-scheduler", daemon=True)
    t.start()
    return t


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    init_db()
    print(f"worker starting (dry_run={get_settings().dry_run})")
    while True:
        print(utcnow().isoformat(), run_once())
        if args.once:
            break
        time.sleep(get_settings().worker_interval_seconds)


if __name__ == "__main__":
    main()
