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
python selftest.py && python edge_check.py && python integration_check.py && python security_check.py && python storage_check.py
```

All five pass at `bc998da`. All offline — no key, no network.

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
| `ai_parser.py` | All Gemini calls: layout classify, OCR, dictation, house-style drafting, transcript proofread. |
| `speech.py` | AI4Bharat Indic speech recognition, hosted or local. |
| `editor/`, `live_dictate/` | Self-contained Streamlit components, plain HTML/JS, no build step. |

## Three things needing the user, not code

1. **Rotate credentials.** The Gemini key, Neon password and access code were pasted
   into a chat transcript. New key at aistudio.google.com, reset `neondb_owner` in Neon,
   new access code, update Streamlit Secrets.
2. **Make the repo private** — currently public. GitHub → Settings → Danger Zone.
   Verified no credential is in any commit, so it is safe meanwhile.
3. **Reboot the Cloud app after any deploy that adds a file or function.** See below.

## Live deployment

- Streamlit Community Cloud, from `max123456-bot/Rdiology-Report-Maker`, branch `main`,
  main file `app.py`.
- Postgres on Neon (`ap-southeast-1`), already migrated to the tenant schema.
- Secrets set in Cloud: `GEMINI_API_KEY`, `STORAGE_URL`, `ACCESS_CODE`.

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
- **Nothing integrates with a PACS or RIS.** Reports come out as `.docx` and stop there.
  This is probably the highest-value next thing and depends entirely on what the clinic
  actually uses.

## Never verified

- **The AI paths have never run against Gemini.** Drafting, transcription, OCR, the
  proofread — all tested offline against their prompt builders. `live_check.py` exercises
  them with `GEMINI_API_KEY` set.
- **No real audio has been dictated.** The whole speech pipeline is unit-tested only.
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
