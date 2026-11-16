"""Read a certificate (PDF or photo) into the flat field dict {key: {"value": str, "confidence": float}}.

Two readers:
  claude      - used whenever ANTHROPIC_API_KEY is set. Sends the PDF (document block) or photo (image block)
                and asks for JSON that matches a fixed schema (structured outputs), with a 0-1 confidence per field.
  local_text  - offline fallback: reads the PDF's text layer with pypdf and pulls fields out with label patterns
                written for ACORD 25-style certificates. It cannot read photos or scanned (image-only) PDFs:
                those come back with confidence 0 so they land in Needs review instead of being guessed.
Every value is normalised by fields.normalize(). Nothing here decides compliance.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass, field

from .config import get_settings
from .fields import FIELD_KEYS, FIELDS, POLICY_TYPES, normalize

IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
PDF = "application/pdf"


@dataclass
class ExtractionResult:
    fields: dict = field(default_factory=dict)
    extractor: str = ""
    error: str = ""
    ms: int = 0
    notes: list = field(default_factory=list)


def media_type_for(filename: str) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext == ".pdf":
        return PDF
    return IMAGE_TYPES.get(ext, "")


def empty_fields(confidence: float = 0.0) -> dict:
    return {k: {"value": "", "confidence": confidence} for k in FIELD_KEYS}


def _clamp(c) -> float:
    try:
        return max(0.0, min(1.0, float(c)))
    except (TypeError, ValueError):
        return 0.0


# --- Claude ---------------------------------------------------------------------------------------------------
def _schema() -> dict:
    fv = {"type": "object", "properties": {"value": {"type": "string"}, "confidence": {"type": "number"}},
          "required": ["value", "confidence"], "additionalProperties": False}
    return {"type": "object", "properties": {k: fv for k in FIELD_KEYS}, "required": list(FIELD_KEYS),
            "additionalProperties": False}


def _instructions() -> str:
    lines = []
    for key, label, kind, group in FIELDS:
        fmt = {"date": "YYYY-MM-DD", "money": "whole dollars, digits only (1000000)", "bool": "yes or no",
               "phone": "as printed", "email": "as printed", "text": "as printed"}[kind]
        lines.append(f"- {key}: {(group + ' - ') if group else ''}{label} ({fmt})")
    return (
        "You read Certificates of Insurance (usually ACORD 25 'Certificate of Liability Insurance').\n"
        "Fill every field below from the document. Use \"\" when the field is not on the document or the policy "
        "type is not listed. Never guess: if text is unreadable, give your best reading and a low confidence.\n"
        "confidence is 0 to 1: 0.95+ only when the value is printed clearly in the expected box; lower it for blurry "
        "or handwritten text, ambiguous rows, or values you had to infer.\n"
        "Policy types: gl = commercial general liability, auto = automobile liability, wc = workers compensation "
        "and employers' liability, umbrella = umbrella or excess liability. carrier = the insurer name for that row "
        "(resolve the INSR LTR letter to the INSURER A-F list). gl.additional_insured = the ADDL INSD column on the "
        "general liability row. producer_* = the agency/producer box (contact email and phone).\n"
        "Fields:\n" + "\n".join(lines)
    )


def extract_with_claude(data: bytes, media_type: str) -> ExtractionResult:
    import anthropic

    s = get_settings()
    client = anthropic.Anthropic(api_key=s.anthropic_api_key or None)
    b64 = base64.standard_b64encode(data).decode()
    block = ({"type": "document", "source": {"type": "base64", "media_type": PDF, "data": b64}}
             if media_type == PDF else
             {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}})
    kwargs = dict(
        model=s.extract_model,
        max_tokens=8000,
        system=_instructions(),
        messages=[{"role": "user", "content": [block, {"type": "text", "text": "Extract the certificate fields."}]}],
        output_config={"effort": s.extract_effort, "format": {"type": "json_schema", "schema": _schema()}},
    )
    t0 = time.time()
    if s.extract_fallbacks and s.extract_model in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"):
        resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    else:
        resp = client.messages.create(**kwargs)
    ms = int((time.time() - t0) * 1000)
    if resp.stop_reason == "refusal":
        return ExtractionResult(fields=empty_fields(), extractor=f"claude:{s.extract_model}",
                                error="model declined to read this document", ms=ms)
    text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
    try:
        raw = json.loads(text)
    except ValueError:
        return ExtractionResult(fields=empty_fields(), extractor=f"claude:{s.extract_model}",
                                error=f"unreadable model output (stop_reason={resp.stop_reason})", ms=ms)
    out = {}
    for k in FIELD_KEYS:
        item = raw.get(k) or {}
        val = normalize(k, item.get("value", ""))
        conf = _clamp(item.get("confidence", 0))
        if item.get("value") and not val:      # model gave something that doesn't parse as a date/amount
            conf = min(conf, 0.3)
        out[k] = {"value": val, "confidence": conf}
    return ExtractionResult(fields=out, extractor=f"claude:{getattr(resp, 'model', s.extract_model)}", ms=ms)


# --- local text-layer reader ------------------------------------------------------------------------------------
DATE_RE = r"\d{1,2}/\d{1,2}/\d{2,4}"
MONEY_RE = r"\$?\s*([\d][\d,\.]*\s*(?:MM|M|K)?)\b"
ROW_TYPES = {
    "gl": r"COMMERCIAL GENERAL LIABILITY|GENERAL LIABILITY",
    "auto": r"AUTOMOBILE LIABILITY|AUTO LIABILITY|BUSINESS AUTO",
    "umbrella": r"UMBRELLA LIAB\w*|EXCESS LIAB\w*",
    "wc": r"WORKERS COMPENSATION|WORKERS' COMPENSATION|WORKERS COMP",
}
LIMIT_PATTERNS = {
    "gl.each_occurrence": r"EACH OCCURRENCE",
    "gl.general_aggregate": r"GENERAL AGGREGATE|GEN'?L AGGREGATE",
    "auto.combined_single_limit": r"COMBINED SINGLE LIMIT",
    "wc.el_each_accident": r"E\.?\s?L\.?\s+EACH ACCIDENT",
    "umbrella.each_occurrence": r"EACH OCCURRENCE",
    "umbrella.aggregate": r"(?<!GENERAL )(?<!COMP/OP )\bAGGREGATE",
}


def pdf_text(data: bytes) -> str:
    import io

    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception:
        return ""


def _line_after(lines: list[str], label_re: str) -> str:
    for i, ln in enumerate(lines):
        if re.fullmatch(label_re, ln.strip(), flags=re.I):
            for nxt in lines[i + 1:i + 4]:
                if nxt.strip():
                    return nxt.strip()
    return ""


def parse_acord_text(text: str) -> dict:
    """Pull fields from an ACORD 25-style text layer. Returns the flat field dict with confidences."""
    out = empty_fields(0.0)
    lines = [ln.rstrip() for ln in text.splitlines()]
    upper = text.upper()

    def put(k, raw, conf):
        v = normalize(k, raw)
        out[k] = {"value": v, "confidence": conf if v else 0.0}

    put("insured_name", _line_after(lines, r"INSURED"), 0.93)
    put("producer_name", _line_after(lines, r"PRODUCER"), 0.92)
    put("certificate_holder", _line_after(lines, r"CERTIFICATE HOLDER"), 0.93)

    m = re.search(r"E-?\s?MAIL(?:\s+ADDRESS)?\s*:\s*([\w.+-]+@[\w-]+\.[\w.-]+)", text, re.I)
    if m:
        put("producer_email", m.group(1), 0.95)
    else:
        m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
        if m:
            put("producer_email", m.group(0), 0.6)
    m = re.search(r"PHONE[^:\n]*:\s*([\d\(\)][\d\(\)\-\.\s]{8,16}\d)", text, re.I)
    if m:
        put("producer_phone", m.group(1), 0.93)

    insurers = {}
    for m in re.finditer(r"INSURER\s+([A-F])\s*:\s*(.+?)(?:\s+(\d{5}))?\s*$", text, re.M):
        insurers[m.group(1)] = m.group(2).strip()

    # coverage rows: split the text from the first coverage row to DESCRIPTION OF OPERATIONS into sections
    start = None
    sections = []   # (type, first_line_index)
    for i, ln in enumerate(lines):
        u = ln.upper()
        if "DESCRIPTION OF OPERATIONS" in u:
            sections.append(("_end", i))
            break
        for t, pat in ROW_TYPES.items():
            if re.search(pat, u) and not any(s[0] == t for s in sections):
                sections.append((t, i))
                start = i if start is None else start
                break
    if not sections or sections[-1][0] != "_end":
        sections.append(("_end", len(lines)))

    for idx, (t, li) in enumerate(sections[:-1]):
        block = lines[li:sections[idx + 1][1]]
        first = block[0]
        joined = "\n".join(block)
        dates = re.findall(DATE_RE, first)
        conf_row = 0.95
        if len(dates) < 2:                       # row wrapped: look in the whole section, lower confidence
            dates = re.findall(DATE_RE, joined)
            conf_row = 0.7
        if len(dates) >= 2:
            put(f"{t}.effective_date", dates[0], conf_row)
            put(f"{t}.expiration_date", dates[1], conf_row)
        # policy number: the token right before the first date
        pm = re.search(r"(\S+)\s+" + re.escape(dates[0]) if dates else r"$^", first if conf_row > 0.9 else joined)
        if pm and re.search(r"\d", pm.group(1)) and not re.fullmatch(DATE_RE, pm.group(1)):
            put(f"{t}.policy_number", pm.group(1), conf_row)
        lm = re.match(r"\s*([A-F])\s", first)
        if lm and lm.group(1) in insurers:
            put(f"{t}.carrier", insurers[lm.group(1)], 0.92)
        elif len(insurers) == 1:
            put(f"{t}.carrier", next(iter(insurers.values())), 0.6)
        if t == "gl":
            # ADDL INSD and SUBR WVD are neighbouring Y/N columns. Two marks -> the first is ADDL INSD.
            # One mark can't be placed from text alone -> low confidence so a human checks the box.
            am = re.search(r"(?:" + ROW_TYPES["gl"] + r")((?:\s+[YNX]\b){0,2})", first.upper())
            marks = am.group(1).split() if am else []
            if len(marks) == 2:
                put("gl.additional_insured", marks[0], 0.9)
            elif len(marks) == 1:
                put("gl.additional_insured", marks[0], 0.5)
            elif out["gl.policy_number"]["value"]:
                put("gl.additional_insured", "no", 0.6)
        for k, pat in LIMIT_PATTERNS.items():
            if not k.startswith(t + "."):
                continue
            mm = re.search(r"(?:" + pat + r")[^\n$\d]*" + MONEY_RE, joined, re.I)
            if mm:
                put(k, mm.group(1), 0.93)

    if not upper.strip():
        return empty_fields(0.0)
    return out


def extract_local(data: bytes, media_type: str) -> ExtractionResult:
    t0 = time.time()
    if media_type != PDF:
        return ExtractionResult(fields=empty_fields(), extractor="local_text", ms=0,
                                error="photo: the offline reader can't read images (set ANTHROPIC_API_KEY)")
    text = pdf_text(data)
    if len(text.strip()) < 80:
        return ExtractionResult(fields=empty_fields(), extractor="local_text", ms=int((time.time() - t0) * 1000),
                                error="no text layer (scanned PDF): set ANTHROPIC_API_KEY to read scans")
    return ExtractionResult(fields=parse_acord_text(text), extractor="local_text", ms=int((time.time() - t0) * 1000))


def claude_available() -> bool:
    return bool(get_settings().anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"))


def extract(data: bytes, media_type: str) -> ExtractionResult:
    s = get_settings()
    if claude_available():
        try:
            return extract_with_claude(data, media_type)
        except Exception as exc:   # API down / bad key: fall back so the upload still gets a status
            if s.local_extractor:
                r = extract_local(data, media_type)
                r.notes.append(f"Claude failed ({exc.__class__.__name__}); used the offline reader")
                return r
            return ExtractionResult(fields=empty_fields(), extractor=f"claude:{s.extract_model}",
                                    error=f"{exc.__class__.__name__}: {exc}"[:500])
    if s.local_extractor:
        return extract_local(data, media_type)
    return ExtractionResult(fields=empty_fields(), extractor="none", error="no extractor configured")


def policy_present(values: dict, t: str) -> bool:
    return bool(values.get(f"{t}.policy_number") or values.get(f"{t}.expiration_date"))


__all__ = ["extract", "ExtractionResult", "media_type_for", "POLICY_TYPES", "policy_present", "claude_available"]
