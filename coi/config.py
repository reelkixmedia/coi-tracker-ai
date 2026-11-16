"""Central settings for the COI tracker. Every value can be overridden by an environment variable or .env file.

Same pattern as the Recovery Engine (pydantic-settings, DRY_RUN=true by default): nothing is emailed to a real
person until DRY_RUN=false. In dry-run every email is written to OUTBOX_DIR and to the database so you can read
exactly what would have gone out (the /outbox page lists them).
"""
from __future__ import annotations

import json
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- core -----------------------------------------------------------------------------------------------
    app_name: str = "COI Tracker"                # placeholder product name
    company_name: str = "Example Co"
    support_email: str = ""
    base_url: str = "http://localhost:8010"      # public URL; sub upload links are built from this
    database_url: str = "sqlite:///./coi.db"
    secret_key: str = "change-me-session-secret"
    dry_run: bool = True
    outbox_dir: str = "./outbox"
    log_file: str = "./coi_events.log"
    upload_dir: str = "./uploads"
    max_upload_mb: int = 15
    admin_emails: str = ""                       # comma-separated logins that can open /admin
    email_allowlist: str = ""                    # limited live mode (same meaning as the Recovery Engine)
    alert_email: str = ""

    # --- automation ("1 click": the web process runs the schedule itself) -----------------------------------
    auto_scheduler: bool = True                  # run the worker loop inside the web process
    worker_interval_seconds: int = 900           # every 15 min: recompute statuses, send due reminders/digests
    reminder_stages: str = "30,14,7,0"           # days before expiration (0 = expiration day)
    digest_weekday: int = 0                      # 0 = Monday
    digest_hour: int = 8                         # local hour of the customer's time zone
    digest_on_free_plan: bool = True             # weekly digest also for free-scan accounts
    expiring_window_days: int = 30               # "Expiring in 30 days" status window

    # --- extraction -------------------------------------------------------------------------------------------
    anthropic_api_key: str = ""
    extract_model: str = "claude-opus-5-5"       # options: claude-sonnet-5-5 (cheaper), claude-haiku-5-5 (cheapest)
    extract_effort: str = "medium"
    extract_fallbacks: bool = True               # server-side refusal fallback (beta) on the Claude API
    review_confidence: float = 0.85              # fields below this are flagged for a human
    local_extractor: bool = True                 # offline text-layer reader when no API key (and for tests)

    # --- email ------------------------------------------------------------------------------------------------
    email_mode: str = "dry_run"                  # dry_run | smtp
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    email_from_address: str = "certificates@example.com"
    mailing_address: str = ""                    # physical address in the email footer (CAN-SPAM)

    # --- billing ----------------------------------------------------------------------------------------------
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    # Price tiers are config only (set by the operator). JSON list; stripe_price = Stripe Price ID; max_vendors 0 = no cap.
    plan_tiers_json: str = (
        '[{"key": "tier1", "label": "Tier 1", "price_text": "$__/mo", "stripe_price": "", "max_vendors": 0},'
        ' {"key": "tier2", "label": "Tier 2", "price_text": "$__/mo", "stripe_price": "", "max_vendors": 0}]'
    )
    free_scan_max_cois: int = 10                 # free scan: upload up to N certificates
    free_bulk_upload: bool = False               # bulk (many files at once) needs a paid plan unless true

    def plan_tiers(self) -> list[dict]:
        try:
            tiers = json.loads(self.plan_tiers_json or "[]")
            return [t for t in tiers if isinstance(t, dict) and t.get("key")]
        except ValueError:
            return []

    def stages(self) -> list[int]:
        out = []
        for part in (self.reminder_stages or "").split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                out.append(int(part))
        return sorted(set(out), reverse=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
