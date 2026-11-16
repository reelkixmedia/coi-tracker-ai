"""The fixed list of fields read from a certificate, plus value normalising shared by every extractor.

Values are stored as strings in a flat dict so review, corrections and accuracy all work field by field:
  dates  -> "YYYY-MM-DD"      money -> "1000000" (whole dollars)      bool -> "yes" | "no"      missing -> ""
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

POLICY_TYPES = [("gl", "General Liability"), ("auto", "Auto Liability"), ("wc", "Workers Comp"),
                ("umbrella", "Umbrella / Excess")]

_TOP = [
    ("insured_name", "Insured (sub / vendor)", "text"),
    ("producer_name", "Agent / producer", "text"),
    ("producer_email", "Agent email", "email"),
    ("producer_phone", "Agent phone", "phone"),
    ("certificate_holder", "Certificate holder", "text"),
]
_LIMITS = {
    "gl": [("additional_insured", "Additional insured (ADDL INSD)", "bool"),
           ("each_occurrence", "Each occurrence", "money"),
           ("general_aggregate", "General aggregate", "money")],
    "auto": [("combined_single_limit", "Combined single limit", "money")],
    "wc": [("el_each_accident", "E.L. each accident", "money")],
    "umbrella": [("each_occurrence", "Each occurrence", "money"), ("aggregate", "Aggregate", "money")],
}


def _build() -> list[tuple[str, str, str, str]]:
    out = [(k, label, kind, "") for k, label, kind in _TOP]
    for t, tlabel in POLICY_TYPES:
        for k, label, kind in [("carrier", "Carrier", "text"), ("policy_number", "Policy number", "text"),
                               ("effective_date", "Effective", "date"), ("expiration_date", "Expires", "date")]:
            out.append((f"{t}.{k}", label, kind, tlabel))
        for k, label, kind in _LIMITS[t]:
            out.append((f"{t}.{k}", label, kind, tlabel))
    return out


FIELDS = _build()                      # (key, label, kind, group)
FIELD_KEYS = [f[0] for f in FIELDS]
FIELD_KIND = {f[0]: f[2] for f in FIELDS}


# --- normalising ------------------------------------------------------------------------------------------------
def norm_date(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def norm_money(raw: str) -> str:
    s = (raw or "").strip().upper().replace("$", "").replace(" ", "")
    if not s:
        return ""
    mult = 1
    if s.endswith("MM") or s.endswith("M"):
        mult, s = 1_000_000, s.rstrip("M")
    elif s.endswith("K"):
        mult, s = 1_000, s[:-1]
    s = s.replace(",", "")
    try:
        return str(int(round(float(s) * mult)))
    except ValueError:
        return ""


def norm_bool(raw: str) -> str:
    s = (raw or "").strip().lower()
    if s in ("y", "yes", "x", "true", "1", "checked"):
        return "yes"
    if s in ("n", "no", "false", "0", "unchecked", ""):
        return "no" if s else ""
    return ""


def norm_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return (raw or "").strip()
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def norm_text(raw: str) -> str:
    return re.sub(r"\s+", " ", (raw or "")).strip()


def normalize(key: str, raw) -> str:
    raw = "" if raw is None else str(raw)
    kind = FIELD_KIND.get(key, "text")
    if kind == "date":
        return norm_date(raw)
    if kind == "money":
        return norm_money(raw)
    if kind == "bool":
        return norm_bool(raw)
    if kind == "phone":
        return norm_phone(raw)
    if kind == "email":
        return norm_text(raw).lower()
    return norm_text(raw)


def as_date(v: str) -> Optional[date]:
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


def as_int(v: str) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def name_key(name: str) -> str:
    """Normalised business name for matching uploads to vendors ("ABC Plumbing, LLC" == "abc plumbing llc")."""
    s = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    s = re.sub(r"\b(llc|inc|co|corp|corporation|company|ltd|the)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()
