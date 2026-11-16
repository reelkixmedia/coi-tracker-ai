import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "events.log"))
    monkeypatch.setenv("AUTO_SCHEDULER", "false")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ADMIN_EMAILS", "owner@example.com")
    from coi import db
    from coi.config import reset_settings_cache

    reset_settings_cache()
    db.reset_engine()
    db.init_db()
    yield tmp_path
    db.reset_engine()
    reset_settings_cache()
