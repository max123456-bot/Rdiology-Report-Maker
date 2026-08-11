# Handoff

State of the project as of `bc998da`. Read this first in a new session.

## What it is

A radiology report formatter for a clinic. Paste (or dictate) a report, get a Word
`.docx` in the client's **HC FORMAT**, with a machine check proving not a single word,
number or measurement was changed.

The guarantee is the point. Everything else is built around not breaking it.

## Run it

```bash
streamlit run app.py
```

```bash
python selftest.py && python edge_check.py && python integration_check.py && python security_check.py && python storage_check.py && python clinical_check.py && python interop_check.py && python pacs_check.py && python master_check.py
```

All nine pass. All offline — no key, no network (pacs_check uses only loopback).

The processing engine is also a service:  `uvicorn api:app` exposes parse /
validate / triage / guidelines / impression / audit / hl7 / fhir over REST
for RIS integration, without touching the Streamlit UI.

## Architecture in one pass

| File | Does |
|---|---|
| `hc_format.py` | `parse_report()` finds structure, `build_docx()` styles it. The HC FORMAT rules live here. |
| `verify.py` | The word-loss audit. Reads the generated `.docx` back **off disk** and compares every word against the source. |
| `validate.py` | Clinical safety checks before sending — a measurement in the impression that is not in the findings, laterality mismatch, leftover `[[uncertain]]`, placeholders. |
| `templates.py` | Per-doctor house style + everything learned about them. |
| `storage.py` | Pluggable persistence: JSON files, SQLite, Postgres. Tenant-scoped. |
| `access.py` | Optional gate: shared `ACCESS_CODE`, or real OIDC sign-in. |
| `dictation_fix.py` | Deterministic post-ASR cleanup: spoken numbers, units, near-miss suggestions. |
| `ai_parser.py` | All Gemini calls: layout classify, OCR, dictation, house-style drafting, transcript proofread, auto-impression, scan pre-read. |
| `speech.py` | AI4Bharat Indic speech recognition, hosted or local. |
| `triage.py` | Deterministic stat/urgent/routine from the clinical text. Negation- and history-aware. Powers the worklist order and alerts. |
| `guidelines.py` | Fleischner 2017 sized from the actual measurement; BI-RADS/TI-RADS/PI-RADS/LI-RADS/O-RADS/Bosniak triggers. Insert is always a click, never automatic. |
| `impression.py` | Rule-based auto-impression: only verbatim sentences from the findings. AI path in `ai_parser.draft_impression`. |
| `records.py` | Report lifecycle draft→signed→delivered with audit trail, patient history keyed on name+sex, measurement comparison against priors. |
| `dicom_meta.py` | Reads a `.dcm` header (pydicom) and cross-checks side, sex, age, name, modality against the report. Metadata only. |
| `interop.py` | HL7 v2.5 ORU^R01 and FHIR R4 DiagnosticReport bundle, both carrying the full audited text. Downloads; no transmitter. |
| `pacs.py` | PACS connectivity both ways: Orthanc REST client (works from Cloud), and pynetdicom C-ECHO / C-FIND / C-STORE receiver with a spool + index (LAN install). `to_png` renders a received slice for the AI pre-read. |
| `notify.py` | Critical result alerts: SMTP email + Twilio SMS/WhatsApp. Builders offline-tested; sending needs `ALERT_SMTP_*` / `TWILIO_*` secrets. Manual click, never automatic. |
| `providers.py` | The AI vendor seam: Gemini built in, another vendor registers a `Provider` and nothing else changes. `AI_PROVIDER` secret selects. |
| `workers.py` | In-memory ThreadPoolExecutor job queue - the scan pre-read runs in the background. |
| `negation.py` | The polarity tripwire: NegEx-style engine; any AI draft that flips a clinical negative ("no pneumothorax" → "pneumothorax") raises `NegationMismatchException` and never reaches the user. Wired into every draft path. |
| `imgprep.py` | OpenCV scan clean-up (bilateral denoise, CLAHE, deskew, adaptive threshold) + hybrid OCR: free local Tesseract first, Gemini Vision only below the 85% confidence bar. |
| `schemas.py` | Pydantic contract for structured outputs: `RadiologyReportSchema`, numbered `ImpressionItem`s with triage-set `is_critical`. |
| `rules_schema.yaml` | Modality-specific validation rules (CT wants TECHNIQUE, mammography wants BI-RADS...) - edit the YAML, not the Python. |
| `api.py` | FastAPI service layer over the offline engine. AI paths deliberately not exposed - nothing unreviewed leaves over HTTP. |
| `editor/`, `live_dictate/` | Self-contained Streamlit components, plain HTML/JS, no build step. |

## Three things needing the user, not code

1. **Rotate credentials.** The Gemini key, Neon password and access code were pasted
   into a chat transcript. New key at aistudio.google.com, reset `neondb_owner` in Neon,
   new access code, update Streamlit Secrets.
2. **Make the repo private** — currently public. GitHub → Settings → Danger Zone.
   Verified no credential is in any commit, so it is safe meanwhile.
3. **Reboot the Cloud app after any deploy that adds a file or function.** See below.

## Live deployment

- **Moving to Render** (Docker runtime — `Dockerfile` + `render.yaml` in the repo;
  walkthrough in DEPLOY.md, including the "Moving off Streamlit Cloud" checklist).
  Docker because the new features need ffmpeg and tesseract-ocr, which Streamlit
  Cloud's and Render's native runtimes cannot both provide.
- Until that switch is done, the old deployment still serves: Streamlit Community
  Cloud, from `max123456-bot/Rdiology-Report-Maker`, branch `main`, main file `app.py`.
  Delete it once Render is verified — two public URLs is two attack surfaces.
- Postgres on Neon (`ap-southeast-1`), already migrated to the tenant schema. The
  database does not move: Render points at the same `STORAGE_URL`.
- Secrets as environment variables on Render: `GEMINI_API_KEY`, `STORAGE_URL`,
  `ACCESS_CODE` (+ optional `ALERT_SMTP_*`, `TWILIO_*`, `ORTHANC_*`, `HF_TOKEN`).

**Check it is healthy:** open Templates → Activity log. It must say
`Storage: Postgres at ep-...`. If it says *JSON files*, the database is not connected and
anything saved will be lost on the next restart.

## The gotcha that bit three times

Streamlit re-executes `app.py` on a rerun but does **not** re-import modules already in
memory. Add a new module or a new function and the running process cannot see it —
locally and on Cloud both.

Symptom: `AttributeError` on something that demonstrably exists in the file.
Fix: restart the server / press **Reboot** on Cloud.

`app.py` now degrades to a banner rather than a crash when this happens, but a reboot is
still the real fix.

## Not built

- **Client-facing multi-tenancy is done at the data layer** — templates, vocabulary and
  learned rules are isolated per tenant, proven on both stores. What is *not* done is
  sign-up, billing, or a per-client admin surface. Tenant comes from the signed-in user's
  email domain; with no sign-in everything is `default`.
- **PACS retrieve and push-back.** The system now connects to a PACS both ways —
  Orthanc REST browsing, C-ECHO, C-FIND queries, and a C-STORE receiver modalities can
  push to (all proven on loopback in `pacs_check.py`). Still not built: C-MOVE bulk
  retrieve, sending the finished report back as a DICOM SR, and an MLLP transmitter for
  the HL7 ORU (it exports as a file today). Those need the clinic's real PACS/RIS to
  test against.
- **An MRN.** Patient history matches on name+sex because that is all a pasted report
  carries. The moment reports arrive with a real patient ID, `records.patient_key`
  should use it.

## Never verified

- **The AI paths have never run against Gemini.** Drafting, transcription, OCR, the
  proofread, the auto-impression, the scan pre-read — all tested offline against their
  prompt builders. `live_check.py` exercises them with `GEMINI_API_KEY` set.
- **No real audio has been dictated.** The whole speech pipeline is unit-tested only.
- **No alert has actually been sent.** SMTP and Twilio senders are real code, tested
  against their builders; a live send needs the `ALERT_SMTP_*` / `TWILIO_*` secrets.
- **No real DICOM from the clinic's machines has been parsed** — only synthetic files
  built in `interop_check.py`.
- **No radiologist has used it.** Every test is synthetic.

## Design decisions worth not re-litigating

- **`PATIENT` is deliberately absent from `MAIN_HEADINGS`.** Reports opening with
  "Patient: <clinical context>" are giving background, not a name. Reviewed and approved.
- **Ambiguous spoken numbers are left as words.** "two fifty" is 250 to an Indian English
  speaker and 52 to an additive parser. A wrong measurement is worse than an unconverted
  one.
- **Near-miss suggestions are never auto-applied.** Silently rewriting a medical term is
  how a wrong word reaches a report.
- **The AI never wins over the audit.** If an AI-classified layout drops a word, the app
  falls back to the rule-based result and says so.
- **Ambiguous dictated ranges stay as words.** "twenty two to three millimeter" is NOT
  converted to "22 to 3 mm" (a descending range no one dictated) - it is left verbatim
  and flagged with both plausible readings. Same principle as "two fifty".
- **A flipped negation is an exception, not a warning.** `negation.assert_polarity` runs
  on every AI draft; a negative that became positive raises and the draft is discarded.
  An omitted negative is only a note - impressions summarise.
- **Reconciliation never auto-applies.** `verify.auto_reconcile` exists and is tested,
  but the UI only ever SHOWS where lost words were - restoring them is the user's click.
