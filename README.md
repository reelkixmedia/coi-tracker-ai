# COI Tracker: AI extraction for Certificates of Insurance

A working prototype that reads Certificates of Insurance (COIs) with AI, checks them against a contractor's
insurance requirements, sends a human only the fields the model is unsure about, and chases renewals
automatically. It also measures its own extraction accuracy against an answer key.

Built by Nikko Echevarria with AI-assisted development (Claude Code).

## The problem

General contractors and property managers must hold a current certificate of insurance for every subcontractor
or vendor on site. Certificates arrive as PDFs, scans and phone photos from many different insurance agencies.
Someone has to read each one, type the policy dates and limits into a spreadsheet, notice when a policy is about
to expire, and email the sub and their agent for a renewal, over and over. Missed expirations mean uninsured work
on site.

## What it does

- **Upload** one or many certificates from the dashboard. Text-based PDFs are read by the built-in reader; scans and photos need the Claude path (built and unit-tested, not yet run live) or go straight to human review.
- **Extract** about 28 fields per certificate: insured, producer/agent contact, certificate holder, and carrier,
  policy number, dates and limits for general liability, auto, umbrella and workers' comp.
- **Match** each certificate to the right subcontractor (finds or creates the vendor by insured name).
- **Status** for every sub against the customer's own requirements: *Compliant*, *Expiring* (within 30 days),
  *Expired*, *Missing requirement* (absent policy, low limit, no additional insured, wrong holder), or
  *Needs review*.
- **Review screen** shows the document next to the fields, with low-confidence fields highlighted. Every human
  change is logged as a correction.
- **Renewal requests** go to the sub and their agent at 30, 14, 7 and 0 days before expiration, each with a
  no-login upload link. A certificate uploaded through that link is read and updates the status by itself.
- **Weekly digest** for the customer: what is expiring, expired, received, and waiting for review.
- **Admin page** with extraction accuracy over time, most-corrected fields, and renewal rate.

Email runs in **dry-run mode** by default: messages are written to an Outbox page, nothing is sent.

## How AI is used

1. **Document extraction with Claude.** When `ANTHROPIC_API_KEY` is set, the PDF (document block) or photo
   (image block) is sent to Claude with a fixed JSON schema (structured outputs). The model returns a value
   **and a 0-1 confidence** for every field. Values are normalised (dates to `YYYY-MM-DD`, money to whole
   dollars, checkboxes to yes/no); a value that does not parse is treated as low confidence. Refusals and
   unreadable output are caught and routed to review instead of guessed.
2. **Offline fallback.** Without a key, a local reader parses the PDF text layer with label patterns. It
   cannot read scans or photos; those return confidence 0 and go to a human.
3. **Confidence scoring and human-in-the-loop.** Fields below `REVIEW_CONFIDENCE` (default 0.85) put the
   certificate in *Needs review*. Everything above it is auto-accepted. No reminders go out for a sub whose
   certificate is unconfirmed.
4. **Correction logging.** Each field a reviewer changes is stored (`field_corrections`, `review_logs`), so
   real-world accuracy can be tracked per field, per extractor and per week on the admin page.
5. **Measuring accuracy.** `tools/accuracy_report.py` runs an extractor over the fake samples, compares every
   field with `samples/ground_truth.json`, and reports accuracy, fields flagged for review, and **silent
   errors** (wrong *and* not flagged), the number that matters most.

### Observed results (local extractor, fake samples)

Run on 2026-11-16 with `tools/accuracy_report.py --extractor local`:

| Metric | Result |
|---|---|
| Field accuracy, all 10 samples | **257 / 280 = 91.8%** |
| Field accuracy, 9 text-layer samples | **251 / 252 = 99.6%** |
| Wrong but flagged for a human | 23 |
| **Silent errors** (wrong and not flagged) | **0** |

The misses are the one scanned, image-only sample (the offline reader cannot read images, so all its fields go
to review) and one "additional insured" checkbox that the text layer cannot disambiguate (flagged at 0.50).
Caveat: the samples are machine-generated and the local reader was written against this layout, so treat 99.6%
as a ceiling, not a forecast. Real-world variety (scans, photos, agency software) is why Claude is the primary
reader.

To compare with Claude (needs an API key; costs API credits):

```
.venv\Scripts\python.exe tools\accuracy_report.py --extractor claude
```

The Claude path is covered by a unit test with a stubbed client (structured output parsing, normalising,
refusal handling). I have not published live-API accuracy numbers here.

## Architecture

```
Upload (dashboard or sub's no-login link)
   -> processing.store_upload()            save file, queue it
   -> processing.process_certificate()     background task
        -> extraction.extract()            Claude (structured JSON + confidence) or local text reader
        -> match to vendor                 by insured name, or by the upload link's vendor
        -> review state                    fields below REVIEW_CONFIDENCE -> Needs review
        -> status.recompute_vendor()       Compliant / Expiring / Expired / Missing / Needs review
Scheduler (in-app, or `python -m coi.worker --once` from cron)
   -> retry unfinished reads -> recompute statuses -> renewal requests 30/14/7/0 -> weekly digest
```

| Module | Role |
|---|---|
| `coi/extraction.py` | Claude and local readers, JSON schema, confidence |
| `coi/fields.py` | The field list and value normalising shared by every reader |
| `coi/processing.py` | Upload pipeline, vendor matching, renewal detection |
| `coi/status.py` | Status rules against the customer's requirements |
| `coi/reminders.py` | Renewal requests (sent once per vendor/expiration/stage) and weekly digest |
| `coi/worker.py` | Scheduled pass; each account in its own transaction |
| `coi/messaging.py` | Email: dry-run outbox or SMTP with an allowlist |
| `coi/billing.py` | Plan tiers and Stripe Checkout/webhooks (config only, unused in dry run) |
| `coi/web/` | FastAPI app and Jinja2 pages (dashboard, review, vendor, outbox, admin) |

## Tech stack

Python 3.11, FastAPI, Jinja2 (server-rendered), SQLAlchemy 2 (SQLite locally, Postgres-ready), pydantic-settings,
Anthropic Python SDK (Claude, structured outputs), pypdf, bcrypt sessions with CSRF tokens, Stripe SDK,
reportlab/Pillow (fake sample generation), pytest.

## How to run

**Windows, one click:** double-click `start_coi_tracker.bat`. On first run it creates `.venv`, installs
`requirements.txt`, copies `.env.example` to `.env`, then starts the app at http://localhost:8010 and opens
the browser.

**Manual:**

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python.exe -m uvicorn coi.web.main:app --port 8010
```

Then sign up at `/signup` (any fake email), save requirements, and upload the PDFs in `samples/bulk20`.
Add `ANTHROPIC_API_KEY` to `.env` to use Claude for extraction; leave it empty to use the offline reader.

Other commands:

| What | Command |
|---|---|
| Regenerate fake samples | `.venv\Scripts\python.exe samples\make_samples.py --bulk 20` |
| One scheduled pass | `.venv\Scripts\python.exe -m coi.worker --once` |
| Bulk-upload a folder | `.venv\Scripts\python.exe tools\upload_folder.py samples\bulk20 --email you@example.com --password "..."` |

## Tests

```
.venv\Scripts\python.exe -m pytest -q
```

**6 tests, all passing** (about 12 s, observed 2026-11-16): the full upload-to-review-to-status flow, reminder
stages and weekly digest, free-plan reminder gating, status rules, the local extractor on the samples, and the
Claude response parser with a stubbed client. No test calls a live API.

## Sample data

Everything in `samples/` is synthetic, generated by `samples/make_samples.py`: invented company, carrier and
person names, `example.com` emails, `555-01xx` phone numbers, and a red "SAMPLE - FAKE DATA" banner on every
page. The 10 ACORD 25-style samples cover clean, expiring, expired, low-limit, missing-policy, wrapped-row,
odd-format and scanned-image cases.

## Limitations

- Prototype. Runs locally only; it has not been deployed or used with real customers.
- Tested only on fake, machine-generated certificates. Accuracy on real certificates is unmeasured.
- Uploaded files are stored on local disk; production would need object storage.
- No password reset yet. Billing is wired to Stripe config but untested against a live account.
- It compares printed dates and limits with the customer's own requirements. **It is not insurance or legal
  advice** and does not judge whether coverage is adequate.

## What I'd do next

- Run the Claude accuracy report on a set of real, consented certificates and tune `REVIEW_CONFIDENCE` from the
  silent-error rate.
- Compare Claude model tiers on accuracy vs. cost per certificate.
- Feed logged corrections back into the prompt and evaluation set.
- Add endorsement (additional insured / waiver) document checks and object storage, then deploy with a cron
  worker.
