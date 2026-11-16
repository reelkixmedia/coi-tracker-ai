"""Make clearly FAKE ACORD 25-style certificates for testing, plus the answer key (ground_truth.json).

Every name, company, carrier, policy number, phone and email is invented (example.com / 555-01xx numbers) and
every page carries a red "SAMPLE - FAKE DATA" banner. Dates are relative to --today so the statuses stay
interesting: some compliant, some expiring soon, one expired, some missing a requirement.

  python samples/make_samples.py                      # 10 samples -> samples/acord25/ (+ ground_truth.json)
  python samples/make_samples.py --bulk 20            # also 20 files -> samples/bulk20/ for the bulk-upload test
"""
from __future__ import annotations

import argparse
import io
import json
import os
import random
import sys
from datetime import date, timedelta

from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from coi.fields import FIELD_KEYS  # noqa: E402

W, H = letter

GL_TYPE = "COMMERCIAL GENERAL LIABILITY"
AUTO_TYPE = "AUTOMOBILE LIABILITY"
UMB_TYPE = "UMBRELLA LIAB"
WC_TYPE = "WORKERS COMPENSATION AND EMPLOYERS' LIABILITY"


def money(n: int, style: str = "comma") -> str:
    if style == "short":
        return f"{n // 1_000_000}M" if n % 1_000_000 == 0 else f"{n // 1000}K"
    if style == "nodollar":
        return f"{n:,}"
    return f"$ {n:,}"


def fmt_date(d: date, style: str = "long") -> str:
    return d.strftime("%m/%d/%y") if style == "short" else d.strftime("%m/%d/%Y")


def draw_cert(c: canvas.Canvas, s: dict) -> None:
    """One ACORD 25-style page. Drawn left column first, then right column, then rows top to bottom."""
    c.setFillColor(HexColor("#b91c1c"))
    c.rect(0, H - 34, W, 34, fill=1, stroke=0)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(W / 2, H - 22, "SAMPLE - FAKE DATA FOR SOFTWARE TESTING - NOT A REAL CERTIFICATE")
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", 15)
    c.drawString(36, H - 60, "CERTIFICATE OF LIABILITY INSURANCE")
    c.setFont("Helvetica", 8)
    c.drawString(430, H - 60, f"DATE (MM/DD/YYYY) {fmt_date(s['issued'])}")
    c.setFont("Helvetica", 6.5)
    c.drawString(36, H - 72, "THIS CERTIFICATE IS ISSUED AS A MATTER OF INFORMATION ONLY AND CONFERS NO RIGHTS UPON THE "
                             "CERTIFICATE HOLDER. (ACORD 25-style layout, test document.)")
    c.rect(30, H - 250, 552, 170)
    c.line(306, H - 250, 306, H - 80)
    # left column: producer then insured
    y = H - 92
    c.setFont("Helvetica-Bold", 8)
    c.drawString(36, y, "PRODUCER")
    c.setFont("Helvetica", 8)
    for ln in [s["producer"], s["producer_addr"], f"CONTACT NAME: {s['producer_contact']}",
               f"PHONE (A/C, No, Ext): {s['producer_phone_printed']}"] + (
              [f"{s['email_label']}: {s['producer_email']}"] if s["producer_email"] else []):
        y -= 11
        c.drawString(36, y, ln)
    y = H - 180
    c.setFont("Helvetica-Bold", 8)
    c.drawString(36, y, "INSURED")
    c.setFont("Helvetica", 8)
    for ln in [s["insured"], s["insured_addr"]]:
        y -= 11
        c.drawString(36, y, ln)
    # right column: insurers
    y = H - 92
    c.setFont("Helvetica-Bold", 8)
    c.drawString(312, y, "INSURER(S) AFFORDING COVERAGE")
    c.setFont("Helvetica", 8)
    for letter_, (name, naic) in s["insurers"].items():
        y -= 12
        c.drawString(312, y, f"INSURER {letter_} : {name} {naic}")
    # coverages
    y = H - 268
    c.setFont("Helvetica-Bold", 9)
    c.drawString(36, y, "COVERAGES")
    y -= 14
    c.setFont("Helvetica-Bold", 6.5)
    c.drawString(36, y, "INSR LTR   TYPE OF INSURANCE   ADDL INSD   SUBR WVD   POLICY NUMBER   POLICY EFF   POLICY EXP   LIMITS")
    c.setFont("Helvetica", 7)
    for row in s["rows"]:
        y -= 16
        c.line(30, y + 11, 582, y + 11)
        first_limit = row["limits"][0] if row["limits"] else ""
        if row.get("wrap"):
            c.drawString(36, y, f"{row['ltr']}  {row['type']}  {row['ai']}  {row['subr']}")
            c.drawString(440, y, first_limit)
            y -= 10
            c.drawString(200, y, f"{row['number']}  {row['eff']}  {row['exp']}")
        else:
            c.drawString(36, y, f"{row['ltr']}  {row['type']}  {row['ai']}  {row['subr']}  {row['number']}  "
                                f"{row['eff']}  {row['exp']}  {first_limit}")
        for lim in row["limits"][1:]:
            y -= 10
            c.drawString(440, y, lim)
    y -= 26
    c.setFont("Helvetica-Bold", 7)
    c.drawString(36, y, "DESCRIPTION OF OPERATIONS / LOCATIONS / VEHICLES")
    c.setFont("Helvetica", 7)
    y -= 10
    c.drawString(36, y, s["description"])
    y -= 40
    c.rect(30, y - 70, 552, 82)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(36, y, "CERTIFICATE HOLDER")
    c.setFont("Helvetica", 8)
    c.drawString(36, y - 12, s["holder"])
    c.drawString(36, y - 23, s["holder_addr"])
    c.drawString(330, y, "CANCELLATION")
    c.setFont("Helvetica", 6.5)
    c.drawString(330, y - 12, "SHOULD ANY OF THE ABOVE POLICIES BE CANCELLED BEFORE THE")
    c.drawString(330, y - 20, "EXPIRATION DATE THEREOF, NOTICE WILL BE DELIVERED PER POLICY PROVISIONS.")
    c.drawString(330, y - 50, "AUTHORIZED REPRESENTATIVE: (signature on file - fake)")


def build_sample(i: int, spec: dict, today: date) -> tuple[dict, dict]:
    """spec -> (drawing dict, ground truth flat dict)."""
    rnd = random.Random(1000 + i)
    eff = today + timedelta(days=spec["exp_in"]) - timedelta(days=365)
    exp = today + timedelta(days=spec["exp_in"])
    ds = spec.get("date_style", "long")
    ms = spec.get("money_style", "comma")
    insurers, rows, truth = {}, [], {}
    letters = iter("ABCDEF")
    holder = spec.get("holder", "Fakeville Builders Group (SAMPLE GC)")
    phone_digits = "555" + f"01{rnd.randint(0, 99):02d}" + f"{rnd.randint(0, 999):03d}"
    p = phone_digits
    phone_printed = {"dash": f"{p[:3]}-{p[3:6]}-{p[6:]}", "dots": f"{p[:3]}.{p[3:6]}.{p[6:]}",
                     "paren": f"({p[:3]}) {p[3:6]}-{p[6:]}"}[spec.get("phone_style", "paren")]
    truth.update({
        "insured_name": spec["insured"],
        "producer_name": spec["producer"],
        "producer_email": spec.get("producer_email", f"certs@{spec['producer_slug']}.example.com"),
        "producer_phone": f"({p[:3]}) {p[3:6]}-{p[6:]}",
        "certificate_holder": holder,
    })
    if spec.get("no_email"):
        truth["producer_email"] = ""

    def add(t, type_label, limits_draw, limits_truth, ai="", carrier=None, pexp=None, wrap=False):
        carrier = carrier or spec["carriers"][len(rows) % len(spec["carriers"])]
        ltr = next((k for k, v in insurers.items() if v[0] == carrier), None)
        if ltr is None:
            ltr = next(letters)
            insurers[ltr] = (carrier, f"{rnd.randint(10000, 99999)}")
        num = f"{t.upper()}-FAKE-{rnd.randint(100000, 999999)}"
        e2 = pexp or exp
        e1 = e2 - timedelta(days=365)
        rows.append({"ltr": ltr, "type": type_label, "ai": ai, "subr": "Y" if t != "auto" else "", "number": num,
                     "eff": fmt_date(e1, ds), "exp": fmt_date(e2, ds), "limits": limits_draw, "wrap": wrap})
        truth[f"{t}.carrier"] = carrier
        truth[f"{t}.policy_number"] = num
        truth[f"{t}.effective_date"] = e1.isoformat()
        truth[f"{t}.expiration_date"] = e2.isoformat()
        truth.update(limits_truth)

    gl_occ, gl_agg = spec.get("gl", (1_000_000, 2_000_000))
    gl_ai = spec.get("gl_ai", "Y")
    add("gl", GL_TYPE, [f"EACH OCCURRENCE {money(gl_occ, ms)}", f"DAMAGE TO RENTED PREMISES {money(100_000, ms)}",
                        f"MED EXP (Any one person) {money(5_000, ms)}", f"PERSONAL & ADV INJURY {money(gl_occ, ms)}",
                        f"GENERAL AGGREGATE {money(gl_agg, ms)}", f"PRODUCTS - COMP/OP AGG {money(gl_agg, ms)}"],
        {"gl.each_occurrence": str(gl_occ), "gl.general_aggregate": str(gl_agg),
         "gl.additional_insured": "yes" if gl_ai == "Y" else "no"},
        ai=gl_ai if gl_ai == "Y" else "", wrap=spec.get("wrap_gl", False),
        pexp=(today + timedelta(days=spec["gl_exp_in"])) if "gl_exp_in" in spec else None)
    if spec.get("auto", True):
        add("auto", AUTO_TYPE, [f"COMBINED SINGLE LIMIT (Ea accident) {money(1_000_000, ms)}"],
            {"auto.combined_single_limit": "1000000"})
    if spec.get("umbrella"):
        u = spec["umbrella"]
        add("umbrella", UMB_TYPE, [f"EACH OCCURRENCE {money(u, ms)}", f"AGGREGATE {money(u, ms)}"],
            {"umbrella.each_occurrence": str(u), "umbrella.aggregate": str(u)})
    if spec.get("wc", True):
        add("wc", WC_TYPE, ["PER STATUTE", f"E.L. EACH ACCIDENT {money(1_000_000, ms)}",
                            f"E.L. DISEASE - EA EMPLOYEE {money(1_000_000, ms)}"],
            {"wc.el_each_accident": "1000000"})
    draw = {
        "issued": today - timedelta(days=rnd.randint(5, 60)),
        "producer": spec["producer"], "producer_addr": f"{rnd.randint(10, 999)} Example Ave, Testville, FL 00000",
        "producer_contact": spec.get("contact", "Pat Sample"), "producer_phone_printed": phone_printed,
        "producer_email": truth["producer_email"], "email_label": spec.get("email_label", "E-MAIL ADDRESS"),
        "insured": spec["insured"], "insured_addr": f"{rnd.randint(10, 999)} Placeholder Rd, Testville, FL 00000",
        "insurers": insurers, "rows": rows, "holder": holder, "holder_addr": "1 Sample Plaza, Testville, FL 00000",
        "description": spec.get("description", "Certificate holder is included as additional insured where required by "
                                               "written contract. FAKE TEST DOCUMENT."),
    }
    full = {k: truth.get(k, "") for k in FIELD_KEYS}
    return draw, full


SPECS = [
    # 1 clean, everything present, compliant
    dict(insured="Testwater Plumbing LLC (SAMPLE)", producer="Example Insurance Agency (FAKE)", producer_slug="example-agency",
         carriers=["Placeholder Mutual Insurance Co (FAKE)", "Sample Casualty Company (FAKE)"], exp_in=210, umbrella=5_000_000,
         note="clean - compliant"),
    # 2 expiring in 12 days, short-year dates, phone with dots
    dict(insured="Mockridge Electric Inc (SAMPLE)", producer="Fictional Risk Partners (FAKE)", producer_slug="fictional-risk",
         carriers=["Imaginary Indemnity Corp (FAKE)"], exp_in=12, date_style="short", phone_style="dots",
         note="expiring in 12 days; MM/DD/YY dates; phone with dots"),
    # 3 GL below requirement
    dict(insured="Dummy Drywall & Paint Co (SAMPLE)", producer="Example Insurance Agency (FAKE)", producer_slug="example-agency",
         carriers=["Placeholder Mutual Insurance Co (FAKE)"], exp_in=150, gl=(500_000, 1_000_000),
         note="GL limits below a 1M/2M requirement"),
    # 4 no workers comp on file
    dict(insured="Notreal Roofing Services (SAMPLE)", producer="Testcase Brokerage LLC (FAKE)", producer_slug="testcase-brokerage",
         carriers=["Sample Casualty Company (FAKE)"], exp_in=300, wc=False, auto=False,
         note="no workers comp, no auto"),
    # 5 expired 9 days ago
    dict(insured="Pretend Concrete Works (SAMPLE)", producer="Fictional Risk Partners (FAKE)", producer_slug="fictional-risk",
         carriers=["Imaginary Indemnity Corp (FAKE)", "Placeholder Mutual Insurance Co (FAKE)"], exp_in=-9,
         note="expired 9 days ago"),
    # 6 not additional insured
    dict(insured="Sampleton HVAC Mechanical (SAMPLE)", producer="Testcase Brokerage LLC (FAKE)", producer_slug="testcase-brokerage",
         carriers=["Sample Casualty Company (FAKE)"], exp_in=95, gl_ai="N",
         description="FAKE TEST DOCUMENT. No additional insured endorsement.",
         note="ADDL INSD not marked on GL"),
    # 7 limits written 1M/2M short, EMAIL label variant, GL expires before the others
    dict(insured="Fauxbuild Framing Crew (SAMPLE)", producer="Example Insurance Agency (FAKE)", producer_slug="example-agency",
         carriers=["Placeholder Mutual Insurance Co (FAKE)"], exp_in=200, gl_exp_in=26, money_style="short",
         email_label="EMAIL", note="limits written as 1M/2M; GL expires in 26 days, others later"),
    # 8 GL row wraps onto two lines (policy number and dates on the next line), no producer email
    dict(insured="Hypothetical Glass & Glazing (SAMPLE)", producer="Mock Coverage Advisors (FAKE)", producer_slug="mock-coverage",
         carriers=["Imaginary Indemnity Corp (FAKE)"], exp_in=180, wrap_gl=True, no_email=True, phone_style="dash",
         note="GL row wrapped onto two lines; no agent email printed"),
    # 9 scanned / image-only PDF (no text layer)
    dict(insured="Ghostline Landscaping (SAMPLE)", producer="Mock Coverage Advisors (FAKE)", producer_slug="mock-coverage",
         carriers=["Sample Casualty Company (FAKE)"], exp_in=120, scanned=True,
         note="scanned image-only PDF (offline reader can't read it; Claude can)"),
    # 10 amounts without $, umbrella, different holder name
    dict(insured="Make-Believe Masonry LLC (SAMPLE)", producer="Testcase Brokerage LLC (FAKE)", producer_slug="testcase-brokerage",
         carriers=["Sample Casualty Company (FAKE)", "Imaginary Indemnity Corp (FAKE)"], exp_in=45, money_style="nodollar",
         umbrella=2_000_000, holder="Nonesuch Property Management (SAMPLE PM)",
         note="amounts without $; umbrella; holder is a different (fake) company"),
]

EXTRA_TRADES = ["Tiling", "Insulation", "Fencing", "Demolition", "Excavation", "Cabinetry", "Flooring", "Siding",
                "Gutters", "Welding", "Steel Erection", "Pool Builders", "Solar Install", "Septic", "Paving"]
EXTRA_PREFIX = ["Fakewood", "Samplestone", "Dummyline", "Mockhaven", "Testbrook", "Notreal", "Placeholder",
                "Fictive", "Pretendo", "Imagin"]


def render_pdf(draw: dict, scanned: bool) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setTitle("SAMPLE FAKE certificate of insurance (test data)")
    draw_cert(c, draw)
    c.showPage()
    c.save()
    data = buf.getvalue()
    if not scanned:
        return data
    # rasterise: render the page to an image and wrap it in an image-only PDF (simulates a phone scan)
    from PIL import Image, ImageFilter
    img = _rasterize(data)
    img = img.rotate(0.8, expand=True, fillcolor="white").filter(ImageFilter.GaussianBlur(0.6))
    out = io.BytesIO()
    c2 = canvas.Canvas(out, pagesize=letter)
    from reportlab.lib.utils import ImageReader
    c2.drawImage(ImageReader(img), 0, 0, W, H)
    c2.showPage()
    c2.save()
    return out.getvalue()


def _rasterize(pdf_bytes: bytes):
    """Draw the same page directly with PIL (no PDF renderer needed): re-run the drawing through a tiny adapter."""
    from PIL import Image, ImageDraw, ImageFont
    scale = 2
    img = Image.new("RGB", (int(W * scale), int(H * scale)), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 14)
    except OSError:
        font = ImageFont.load_default()
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes))
    parts = []
    reader.pages[0].extract_text(visitor_text=lambda t, cm, tm, fd, fs: parts.append((t, tm[4], tm[5])))
    for t, x, y in parts:
        if t.strip():
            d.text((x * scale, (H - y) * scale - 12), t.strip(), fill="black", font=font)
    return img


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--today", default=date.today().isoformat())
    ap.add_argument("--bulk", type=int, default=0)
    args = ap.parse_args()
    today = date.fromisoformat(args.today)
    out_dir = os.path.join(HERE, "acord25")
    os.makedirs(out_dir, exist_ok=True)
    truth = {"generated_for": today.isoformat(), "samples": {}}
    for i, spec in enumerate(SPECS, start=1):
        draw, full = build_sample(i, spec, today)
        name = f"FAKE_COI_{i:02d}_{spec['insured'].split(' (')[0].replace(' ', '_').replace('&', 'and')}.pdf"
        with open(os.path.join(out_dir, name), "wb") as fh:
            fh.write(render_pdf(draw, spec.get("scanned", False)))
        truth["samples"][name] = {"note": spec["note"], "scanned": bool(spec.get("scanned")), "fields": full}
    with open(os.path.join(HERE, "ground_truth.json"), "w", encoding="utf-8") as fh:
        json.dump(truth, fh, indent=2)
    print(f"wrote {len(SPECS)} samples to {out_dir}")

    if args.bulk:
        bulk_dir = os.path.join(HERE, f"bulk{args.bulk}")
        os.makedirs(bulk_dir, exist_ok=True)
        rnd = random.Random(7)
        bulk_truth = {}
        for n in range(args.bulk):
            if n < len(SPECS):
                spec = SPECS[n]
            else:
                base = dict(SPECS[n % len(SPECS)])
                base.pop("scanned", None)
                base["insured"] = f"{EXTRA_PREFIX[n % len(EXTRA_PREFIX)]} {EXTRA_TRADES[n % len(EXTRA_TRADES)]} LLC (SAMPLE)"
                base["exp_in"] = rnd.choice([-3, 5, 13, 29, 60, 120, 240, 330])
                base.pop("gl_exp_in", None)
                spec = base
            draw, full = build_sample(100 + n, spec, today)
            name = f"FAKE_BULK_{n + 1:02d}_{spec['insured'].split(' (')[0].replace(' ', '_').replace('&', 'and')}.pdf"
            with open(os.path.join(bulk_dir, name), "wb") as fh:
                fh.write(render_pdf(draw, spec.get("scanned", False)))
            bulk_truth[name] = {"scanned": bool(spec.get("scanned")), "fields": full}
        with open(os.path.join(bulk_dir, "ground_truth_bulk.json"), "w", encoding="utf-8") as fh:
            json.dump({"generated_for": today.isoformat(), "samples": bulk_truth}, fh, indent=2)
        print(f"wrote {args.bulk} bulk files to {bulk_dir}")


if __name__ == "__main__":
    main()
