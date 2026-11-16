"""Extraction accuracy on the fake samples: runs the extractor on every PDF and compares each field with
samples/ground_truth.json. Writes reports/extraction_accuracy_<timestamp>.md (never overwrites an older report).

  .venv\\Scripts\\python.exe tools\\accuracy_report.py                 # whichever extractor the app would use
  .venv\\Scripts\\python.exe tools\\accuracy_report.py --extractor local
  .venv\\Scripts\\python.exe tools\\accuracy_report.py --extractor claude   # needs ANTHROPIC_API_KEY (costs API money)

"Correct" = normalised value equals the answer key (an empty field that should be empty counts as correct).
"Flagged" = fields the app highlights for a human on the review screen (confidence below REVIEW_CONFIDENCE).
"Silent error" = wrong AND not flagged: the number that matters most (a wrong value nobody is asked to check).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from coi import extraction  # noqa: E402
from coi.config import get_settings  # noqa: E402
from coi.fields import FIELD_KEYS  # noqa: E402
from coi.processing import review_state_for  # noqa: E402


def run(sample_dir: str, truth_path: str, which: str) -> dict:
    truth = json.load(open(truth_path, encoding="utf-8"))["samples"]
    thr = get_settings().review_confidence
    rows, per_field = [], {k: [0, 0] for k in FIELD_KEYS}
    for name, t in sorted(truth.items()):
        data = open(os.path.join(sample_dir, name), "rb").read()
        if which == "local":
            r = extraction.extract_local(data, extraction.PDF)
        elif which == "claude":
            r = extraction.extract_with_claude(data, extraction.PDF)
        else:
            r = extraction.extract(data, extraction.PDF)
        ok = silent = wrong_flagged = 0
        misses = []
        # flagged = exactly the fields the app sends to a human (same rule as processing.review_state_for)
        _, low_list = review_state_for(r.fields, None)
        if r.error and not any(v.get("value") for v in r.fields.values()):
            low_list = list(FIELD_KEYS)
        flagged = len(low_list)
        for k in FIELD_KEYS:
            want = t["fields"].get(k, "")
            got = r.fields.get(k, {}).get("value", "")
            conf = r.fields.get(k, {}).get("confidence", 0.0)
            low = k in low_list or conf < thr
            per_field[k][1] += 1
            if got == want:
                ok += 1
                per_field[k][0] += 1
            else:
                misses.append((k, want, got, conf))
                if low:
                    wrong_flagged += 1
                else:
                    silent += 1
        rows.append(dict(name=name, note=t.get("note", ""), extractor=r.extractor, error=r.error, ok=ok,
                         total=len(FIELD_KEYS), flagged=flagged, silent=silent, wrong_flagged=wrong_flagged,
                         misses=misses, ms=r.ms))
    return {"rows": rows, "per_field": per_field, "threshold": thr}


def write_md(res: dict, out_path: str, which: str) -> None:
    rows = res["rows"]
    tot = sum(r["total"] for r in rows)
    ok = sum(r["ok"] for r in rows)
    silent = sum(r["silent"] for r in rows)
    wf = sum(r["wrong_flagged"] for r in rows)
    text_rows = [r for r in rows if "scanned" not in r["note"]]
    t_tot = sum(r["total"] for r in text_rows) or 1
    t_ok = sum(r["ok"] for r in text_rows)
    L = [f"# Extraction accuracy on the fake samples ({datetime.now():%Y-%m-%d %H:%M})", "",
         f"Extractor: **{rows[0]['extractor'] if rows else which}** · review threshold {res['threshold']}", "",
         f"- Field accuracy, all {len(rows)} samples: **{ok}/{tot} = {ok / tot:.1%}**",
         f"- Field accuracy, text-layer samples only ({len(text_rows)}): **{t_ok}/{t_tot} = {t_ok / t_tot:.1%}**",
         f"- Wrong but flagged for review (a human is asked): {wf}",
         f"- **Silent errors (wrong and NOT flagged): {silent}**", "",
         "| Sample | What it tests | Correct | Flagged | Silent errors | Note |", "|---|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| {r['name']} | {r['note']} | {r['ok']}/{r['total']} | {r['flagged']} | {r['silent']} | "
                 f"{r['error'] or ''} |")
    L += ["", "## Every wrong field", "", "| Sample | Field | Answer key | Read as | Confidence |", "|---|---|---|---|---|"]
    for r in rows:
        for k, want, got, conf in r["misses"]:
            L.append(f"| {r['name']} | {k} | {want or '(empty)'} | {got or '(empty)'} | {conf:.2f} |")
    L += ["", "## Accuracy by field", "", "| Field | Correct |", "|---|---|"]
    for k, (a, b) in res["per_field"].items():
        L.append(f"| {k} | {a}/{b} |")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extractor", choices=["auto", "local", "claude"], default="auto")
    ap.add_argument("--samples", default=os.path.join(ROOT, "samples", "acord25"))
    ap.add_argument("--truth", default=os.path.join(ROOT, "samples", "ground_truth.json"))
    args = ap.parse_args()
    res = run(args.samples, args.truth, args.extractor)
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    out = os.path.join(ROOT, "reports", f"extraction_accuracy_{args.extractor}_{datetime.now():%Y%m%d_%H%M%S}.md")
    write_md(res, out, args.extractor)
    rows = res["rows"]
    print(f"{sum(r['ok'] for r in rows)}/{sum(r['total'] for r in rows)} fields correct, "
          f"{sum(r['silent'] for r in rows)} silent errors -> {out}")
    for r in rows:
        for m in r["misses"]:
            print("  MISS", r["name"][:28], m)


if __name__ == "__main__":
    main()
