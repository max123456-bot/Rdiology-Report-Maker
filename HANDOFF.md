# Handoff

State of the project as of the audit/parity pass on 2026-08-11 (after
`6e8eb14`). Read this first in a new session.

## Since the last handoff, in one paragraph

A full audit-and-parity pass over the whole app. Security review found the
code clean (parameterised SQL throughout, `yaml.safe_load`, no eval/exec/
pickle, one benign `unsafe_allow_html` on a static docstring) and one real
bug: a mistyped `STORAGE_URL` silently became a SQLite file named after the
typo — `SqlStore` now refuses URLs without a `postgresql://`/`sqlite:///`
scheme, and the stray `your-neon-string` file (empty, but committed) is
untracked and gitignored. Backend features that never reached the UI now
have one: a DICOMweb (QIDO-RS/WADO-RS) browser in the Worklist PACS section
(with new `pacs.wado_first_instance`), a macro add/remove editor on the
template editor, attestation-chain verification inside the Activity log
(new `verify.audit_chain_status`), a sidebar protection-status line (PHI
encryption / cloud de-id / signing key), and a show-restored-text toggle on
the word-loss audit. The service API gained `/anatomy` and
`/dictation-cleanup`. Typography was overhauled (system font stacks in
`.streamlit/config.toml`, tabular numerals so measurements align, tighter
heading tracking). The template manager moved into the sidebar under
Doctor/template. Thirteen offline check suites, all green; run them with the
command below. Deployment target is Render (Docker) - see DEPLOY.md; the old
Streamlit Cloud app should be deleted once Render is verified. Still pending
on the user: rotate credentials, make the repo private.

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
python selftest.py && python edge_check.py && python integration_check.py && python security_check.py && python storage_check.py && python clinical_check.py && python interop_check.py && python pacs_check.py && python master_check.py && python enterprise_check.py && python parsing_check.py && python stt_check.py && python batch_check.py
```

All thirteen pass. All offline — no key, no network (pacs/enterprise/stt checks use only loopback).

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
| `stt.py` | Pluggable fast STT: Sarvam Saarika (Indian, code-mixed, ~1s), ElevenLabs Scribe, and ANY OpenAI-compatible endpoint (Groq Whisper, local faster-whisper) via `CUSTOM_STT_URL`. Appears in the Dictate tab when its key is set; every result shows measured latency; same ITN/lexicon/layout pipeline downstream. |
| `batch.py` | Bulk engine behind the Batch tab: CSV/XLSX intake (metadata columns become report headings), per-report doctor auto-routing by signature (ambiguity falls back, never guesses), per-row error containment, pre-download QC dashboard, and a SHA-256 `batch_audit_manifest.csv` inside every ZIP. |
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
| `api.py` | FastAPI service layer over the offline engine. AI paths deliberately not exposed - nothing unreviewed leaves over HTTP. Now also `/anatomy` (findings tree) and `/dictation-cleanup` (post-ASR ITN, suggestions advisory). |
| `crypto.py` | PHI at rest: with `PHI_KEY`, report payloads are one AES-256-GCM sealed blob; only index columns stay plaintext, and `patient_key` becomes a keyed hash. Losing the key loses the records. |
| `mllp.py` | HL7 over TCP: ORM^O01 order listener (orders become worklist drafts, LAN only) and ACK-gated ORU push (`MLLP_HOST`/`MLLP_PORT`). |
| `deid.py` | De-identification before the cloud (`DEID_CLOUD`): known names, MRN/UHID, phones, emails, dates become placeholders; the model's answer is re-identified locally. |
| `ollama.py` | The air-gapped AI path: same prompts, same negation tripwire, local model via `OLLAMA_URL`. Vision stays Gemini-only, honestly. |
| RBAC (`access.py`) | Roles via `ROLE_MAP`/`ROLE_DEFAULT`; unconfigured = everyone attending (solo-clinic behaviour). Only attending signs; signing past a critical flag demands a written justification, recorded on the trail and in the audit log. |
| Signatures (`verify.py`) | With `ATTEST_KEY`: HMAC-SHA256 over each signed report's exact text + a signed attestation chain — non-repudiation, not just integrity. |
| MWL / DICOMweb (`pacs.py`) | Modality Worklist C-FIND (pick the scheduled patient instead of typing), QIDO-RS study search and WADO-RS instance fetch via `DICOMWEB_URL`. |
| `anatomy.py` | Findings as a REGION → ORGAN → SUBPART tree plus deterministic coreference ("It measures 2.5 cm" attaches to the organ in context). A structured VIEW — the document text is never touched. |
| Self-correction flag (`validate.py`) | "…12 mm mass, wait, left kidney…" is flagged critical: no software may pick a half, the human reviews. |
| Table reconstruction (`readers.py`) | pdfplumber rebuilds echo/lab grids as `Header: value` lines, clearly marked, deletable. |
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

- **Most AI paths have never run against Gemini** — transcription, OCR, the proofread,
  the auto-impression, the scan pre-read are tested offline against their prompt
  builders; `live_check.py` exercises them with `GEMINI_API_KEY` set. **Exception,
  verified live 2026-08-11:** the drafting path (`draft_with_questions`) ran against
  real Gemini from the Draft tab — shorthand notes came back as a numbered impression
  with every negation carried and the measurements badge green.
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
