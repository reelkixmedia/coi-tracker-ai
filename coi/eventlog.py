"""Append-only tracking (same as the Recovery Engine): every action goes to event_log AND a JSON-lines file."""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy.orm import Session

from .config import get_settings
from .db import utcnow
from .models import EventLog


def log_event(session: Session, type_: str, account_id: Optional[int] = None, **detail: Any) -> None:
    clean = {k: (v if isinstance(v, (str, int, float, bool, type(None), list, dict)) else str(v))
             for k, v in detail.items()}
    session.add(EventLog(account_id=account_id, type=type_, detail=clean))
    try:
        with open(get_settings().log_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": utcnow().isoformat(), "type": type_, "account_id": account_id, **clean},
                                default=str) + "\n")
    except OSError:
        pass
