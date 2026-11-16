"""End-to-end: the 'Done means' list, run against freshly generated fake samples dated from today."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from samples import make_samples


def _samples(n: int, today: date) -> list[tuple[str, bytes, dict]]:
    out = []
    for i in range(n):
        spec = dict(make_samples.SPECS[i % len(make_samples.SPECS)])
        if i >= len(make_samples.SPECS):
            spec.pop("scanned", None)
            spec.pop("gl_exp_in", None)
            spec["insured"] = f"Extra Fake Sub {i} LLC (SAMPLE)"
            spec["exp_in"] = [40, 13, 200, -2, 90, 6, 300, 25, 150, 365][i % 10]
        draw, truth = make_samples.build_sample(100 + i, spec, today)
        out.append((f"FAKE_{i:02d}.pdf", make_samples.render_pdf(draw, spec.get("scanned", False)), truth))
    return out


def _csrf(client, path="/signup"):
    html = client.get(path).text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


@pytest.fixture()
def client(env):
    from fastapi.testclient import TestClient
    from coi.web.main import create_app

    with TestClient(create_app()) as c:
        yield c


def _signup(client, email="owner@example.com"):
    tok = _csrf(client)
    r = client.post("/signup", data={"company": "Fakeville Builders Group (SAMPLE GC)", "email": email,
                                     "password": "testpass123", "timezone": "America/New_York", "csrf": tok},
                    follow_redirects=False)
    assert r.status_code == 303 and "/setup" in r.headers["location"]
    return _csrf(client, "/setup")   # login starts a fresh session (new CSRF token)


def test_full_flow(client, env):
    from coi.db import session_scope
    from coi.models import Account, Certificate, FieldCorrection, OutboundEmail, ReminderSent, Renewal, Vendor
    from coi import worker

    tok = _signup(client)
    # requirements
    r = client.post("/setup", data={"csrf": tok, "gl_each_occurrence": "1M", "gl_general_aggregate": "2,000,000",
                                    "auto_combined_single_limit": "0", "umbrella_each_occurrence": "0",
                                    "wc_required": "1", "additional_insured_required": "1",
                                    "holder_name_contains": "", "reply_email": "owner@example.com",
                                    "timezone": "America/New_York", "reminders_enabled": "1", "digest_enabled": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    # free scan blocks bulk
    today = date.today()
    files = _samples(20, today)
    r = client.post("/upload", data={"csrf": tok}, files=[("files", (n, b, "application/pdf")) for n, b, _ in files[:2]])
    assert "Bulk upload is on paid plans" in r.text
    # local test plan, then bulk-upload 20
    client.post("/billing/dev-activate", data={"csrf": tok, "plan": "tier1"})
    r = client.post("/upload", data={"csrf": tok}, files=[("files", (n, b, "application/pdf")) for n, b, _ in files])
    assert r.status_code == 200 and "Reading 20 certificate(s)" in r.text
    with session_scope() as s:
        certs = s.scalars(select(Certificate)).all()
        assert len(certs) == 20 and all(c.extraction_status == "done" for c in certs)
        vendors = s.scalars(select(Vendor)).all()
        assert len(vendors) == 20
        by_name = {v.name: v for v in vendors}
        assert by_name["Testwater Plumbing LLC (SAMPLE)"].status == "compliant"
        assert by_name["Mockridge Electric Inc (SAMPLE)"].status == "expiring"
        assert by_name["Pretend Concrete Works (SAMPLE)"].status == "expired"
        assert by_name["Dummy Drywall & Paint Co (SAMPLE)"].status == "missing"
        assert by_name["Notreal Roofing Services (SAMPLE)"].status == "missing"      # no WC
        assert by_name["Sampleton HVAC Mechanical (SAMPLE)"].status == "needs_review"  # ADDL INSD unclear
        scanned = [v for v in vendors if v.name.startswith("Unreadable certificate")]
        assert len(scanned) == 1 and scanned[0].status == "needs_review"
        assert by_name["Testwater Plumbing LLC (SAMPLE)"].agent_email == "certs@example-agency.example.com"
        review_ids = [c.id for c in certs if c.review_state == "needs_review"]
        auto = sum(1 for c in certs if c.review_state == "auto_accepted")
        assert auto >= 15
        sampleton = next(c for c in certs if c.vendor_id == by_name["Sampleton HVAC Mechanical (SAMPLE)"].id)
        sampleton_id, sampleton_vid = sampleton.id, sampleton.vendor_id
        scanned_cert = next(c for c in certs if c.vendor_id == scanned[0].id)
    # dashboard
    html = client.get("/dashboard").text
    assert "Expired" in html and "Pretend Concrete Works" in html
    # correct an extraction (Sampleton: ADDL INSD is actually not marked) -> logged as a correction
    page = client.get(f"/certificates/{sampleton_id}").text
    form = dict(re.findall(r'name="([a-z_.]+)" type="text" value="([^"]*)"', page))
    form["csrf"] = tok   # checkbox gl.additional_insured left unchecked = "no"
    r = client.post(f"/certificates/{sampleton_id}", data=form, follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as s:
        fc = s.scalars(select(FieldCorrection)).all()
        assert [f.field for f in fc] == ["gl.additional_insured"]
        assert s.get(Vendor, sampleton_vid).status == "missing"     # now confirmed: not additional insured
    # scanned one: a human types the fields in -> vendor renamed from the placeholder
    truth_scan = files[8][2]
    form = {k: v for k, v in truth_scan.items() if k != "gl.additional_insured"}
    form["gl.additional_insured"] = "yes"
    form["csrf"] = tok
    client.post(f"/certificates/{scanned_cert.id}", data=form)
    with session_scope() as s:
        v = s.get(Vendor, scanned[0].id)
        assert v.name == "Ghostline Landscaping (SAMPLE)" and v.status == "compliant"
    # test reminder email with a working sub upload link
    with session_scope() as s:
        mock = s.scalar(select(Vendor).where(Vendor.name == "Mockridge Electric Inc (SAMPLE)"))
        mid, token = mock.id, mock.upload_token
    r = client.post(f"/vendors/{mid}/test-reminder", data={"csrf": tok})
    assert "Test renewal request written to the outbox" in r.text
    with session_scope() as s:
        mail = s.scalar(select(OutboundEmail).where(OutboundEmail.kind == "renewal_test"))
        link = re.search(r"https?://\S+/u/\S+", mail.body).group(0)
    path = link.split("8010", 1)[-1] if "8010" in link else "/u/" + token
    sub = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(client.app)
    page = sub.get(path)
    assert page.status_code == 200 and "Mockridge Electric" in page.text
    sub_tok = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    # sub uploads a renewed certificate (expires in a year) -> read + status update with no clicks
    spec = dict(make_samples.SPECS[1], exp_in=365)
    draw, _ = make_samples.build_sample(555, spec, today)
    r = sub.post(path, data={"csrf": sub_tok}, files=[("files", ("renewal.pdf", make_samples.render_pdf(draw, False),
                                                                 "application/pdf"))])
    assert "Thank you - received" in r.text
    with session_scope() as s:
        v = s.get(Vendor, mid)
        assert v.status == "compliant", v.status_reasons
        ren = s.scalars(select(Renewal)).all()
        assert len(ren) == 1 and ren[0].source == "sub_link"
    # scheduled pass: due reminders go out once, never twice
    worker.run_once()
    worker.run_once()
    with session_scope() as s:
        sent = s.scalars(select(ReminderSent)).all()
        keys = [(r.vendor_id, r.expiration_date, r.stage) for r in sent]
        assert len(keys) == len(set(keys)) and len(sent) >= 4
        stages = {r.stage for r in sent}
        assert stages <= {30, 14, 7, 0}
    # admin page renders for the admin login
    assert "Extraction accuracy over time" in client.get("/admin").text


def test_reminder_stages_and_digest(env):
    from coi import reminders, status
    from coi.db import session_scope
    from coi.models import Account, Certificate, ReminderSent, Vendor

    assert reminders.due_stage(31, [30, 14, 7, 0]) is None
    assert reminders.due_stage(30, [30, 14, 7, 0]) == 30
    assert reminders.due_stage(12, [30, 14, 7, 0]) == 14
    assert reminders.due_stage(0, [30, 14, 7, 0]) == 0
    assert reminders.due_stage(-4, [30, 14, 7, 0]) == 0
    with session_scope() as s:
        a = Account(name="Fake GC (SAMPLE)", reply_email="gc@example.com", plan_status="active", requirements={})
        s.add(a)
        s.flush()
        v = Vendor(account_id=a.id, name="Fake Sub (SAMPLE)", name_key="fake sub sample", agent_email="agent@example.com")
        s.add(v)
        s.flush()
        today = status.local_today(a)
        exp = today + timedelta(days=30)
        c = Certificate(account_id=a.id, vendor_id=v.id, extraction_status="done", review_state="auto_accepted",
                        extracted={"gl.policy_number": {"value": "GL-1", "confidence": 1},
                                   "gl.expiration_date": {"value": exp.isoformat(), "confidence": 1},
                                   "gl.each_occurrence": {"value": "1000000", "confidence": 1},
                                   "gl.general_aggregate": {"value": "2000000", "confidence": 1},
                                   "gl.additional_insured": {"value": "yes", "confidence": 1},
                                   "wc.policy_number": {"value": "WC-1", "confidence": 1},
                                   "wc.expiration_date": {"value": exp.isoformat(), "confidence": 1}})
        s.add(c)
        s.flush()
        aid = a.id
    # walk the calendar day by day across the whole window: exactly one email per stage
    base = datetime.utcnow().replace(hour=15)
    for d in range(0, 40):
        now = base + timedelta(days=d)
        with session_scope() as s:
            a = s.get(Account, aid)
            status.recompute_account(s, a, now)
            s.flush()
            reminders.process_account(s, a, now)
    with session_scope() as s:
        assert sorted(r.stage for r in s.scalars(select(ReminderSent)).all()) == [0, 7, 14, 30]
        a = s.get(Account, aid)
        # digest: due on Monday 8am local, once per week
        monday = base
        while monday.weekday() != 0:
            monday += timedelta(days=1)
        monday = monday.replace(hour=14)   # 9-10am Eastern
        assert reminders.maybe_send_digest(s, a, monday) is True
        s.flush()
        assert reminders.maybe_send_digest(s, a, monday + timedelta(hours=2)) is False


def test_free_plan_reminders_wait(env):
    from coi import reminders, status
    from coi.db import session_scope
    from coi.models import Account, EventLog, ReminderSent, Vendor

    with session_scope() as s:
        a = Account(name="Free GC", reply_email="gc@example.com", plan_status="free", requirements={})
        s.add(a)
        s.flush()
        today = status.local_today(a)
        s.add(Vendor(account_id=a.id, name="Sub", name_key="sub", agent_email="a@example.com", status="expiring",
                     next_expiration=today + timedelta(days=5)))
        s.flush()
        out = reminders.process_account(s, a)
        assert out["skipped_plan"] == 1
        assert s.scalar(select(func.count(ReminderSent.id))) == 0


def test_status_rules():
    from coi.status import evaluate

    req = {"gl_each_occurrence": 1_000_000, "gl_general_aggregate": 2_000_000, "wc_required": True,
           "additional_insured_required": True}
    today = date(2026, 11, 8)
    good = {"gl.policy_number": "G", "gl.expiration_date": "2027-06-01", "gl.each_occurrence": "1000000",
            "gl.general_aggregate": "2000000", "gl.additional_insured": "yes", "wc.policy_number": "W",
            "wc.expiration_date": "2027-03-01"}
    assert evaluate(good, req, today)[0] == "compliant"
    assert evaluate({**good, "wc.expiration_date": "2026-11-20"}, req, today)[0] == "expiring"
    assert evaluate({**good, "wc.expiration_date": "2026-11-01"}, req, today)[0] == "expired"
    assert evaluate({**good, "gl.each_occurrence": "500000"}, req, today)[0] == "missing"
    no_wc = {k: v for k, v in good.items() if not k.startswith("wc.")}
    assert evaluate(no_wc, req, today)[0] == "missing"
    assert evaluate(good, req, today)[2] == date(2027, 3, 1)


def test_local_extractor_on_samples():
    from coi import extraction
    from coi.config import get_settings

    today = date.today()
    thr = get_settings().review_confidence
    silent = 0
    for i, spec in enumerate(make_samples.SPECS):
        draw, truth = make_samples.build_sample(i + 1, spec, today)
        r = extraction.extract_local(make_samples.render_pdf(draw, spec.get("scanned", False)), extraction.PDF)
        for k, want in truth.items():
            got = r.fields[k]
            if got["value"] != want and got["confidence"] >= thr:
                silent += 1
    assert silent == 0


def test_claude_path_parses_structured_output(monkeypatch):
    """No API call: a fake client returns schema-shaped JSON; checks normalising, confidences and refusals."""
    import json as _json
    import types

    from coi import extraction
    from coi.fields import FIELD_KEYS

    payload = {k: {"value": "", "confidence": 0.99} for k in FIELD_KEYS}
    payload["gl.expiration_date"] = {"value": "03/04/2027", "confidence": 0.97}
    payload["gl.each_occurrence"] = {"value": "$1,000,000", "confidence": 0.96}
    payload["producer_phone"] = {"value": "555.010.2222", "confidence": 0.7}

    class FakeMessages:
        def __init__(self, stop="end_turn"):
            self.stop = stop
            self.kwargs = None

        def create(self, **kw):
            self.kwargs = kw
            return types.SimpleNamespace(stop_reason=self.stop, model=kw["model"],
                                         content=[types.SimpleNamespace(type="text", text=_json.dumps(payload))])

    fm = FakeMessages()
    fake = types.SimpleNamespace(messages=fm, beta=types.SimpleNamespace(messages=fm))
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)
    r = extraction.extract_with_claude(b"%PDF-1.4 fake", extraction.PDF)
    assert r.fields["gl.expiration_date"]["value"] == "2027-03-04"
    assert r.fields["gl.each_occurrence"]["value"] == "1000000"
    assert r.fields["producer_phone"] == {"value": "(555) 010-2222", "confidence": 0.7}
    assert fm.kwargs["output_config"]["format"]["type"] == "json_schema"
    assert fm.kwargs["messages"][0]["content"][0]["type"] == "document"
    fm.stop = "refusal"
    r = extraction.extract_with_claude(b"%PDF-1.4 fake", extraction.PDF)
    assert r.error and all(v["confidence"] == 0 for v in r.fields.values())
