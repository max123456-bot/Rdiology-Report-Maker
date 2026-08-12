# HC Format Radiology Report Generator

Paste whatever your boss sends — messy text, a Word file, a PDF, a photo of a report — and
get back a Microsoft Word `.docx` in **HC FORMAT**, with a machine check proving that not a
single word, number or measurement was changed, summarised or dropped.

---

## Quick start

```bash
pip install -r requirements.txt
```

```bash
streamlit run app.py
```

The app opens at http://localhost:8501. On Windows you can just double-click `run.bat`.

No API key is needed for the default engine.

---

## The two engines

| | Rule-based (default) | AI-assisted |
|---|---|---|
| API key | none | one key configured once, for everyone (see below) |
| Internet | not needed | needed |
| Speed | instant | ~1–3 s per report |
| Word-loss risk | **structurally zero** — it only applies styling, it never rewrites text | checked, and rejected if it drifts |
| Best for | reports with recognisable headings (almost all of them) | badly formatted or unstructured dumps, and OCR of scans/photos |

The AI is used **only as a layout classifier** — it decides which line is a heading, a finding
or an impression point. Its output is then run through the same word-loss audit, and if even
one word differs from your paste, the app silently falls back to the rule-based result and
tells you it did. The guarantee holds either way.

Model IDs are **not hardcoded**. The app asks Google which models your key can call, caches the
answer in `.model_cache.json`, and refreshes it weekly — so the picker is populated on every
load with nothing to click. *Refresh model list* forces it early, for the day Google ships a
model you want immediately. A failed refresh keeps serving the cached list rather than emptying
the picker, and you can always type a model ID by hand.

### Setting up the API key, once

Nobody pastes a key into the app — there is no field for it. The key is configured once and
every session picks it up automatically.

```bash
copy secrets.toml.example .streamlit\secrets.toml
```

Open `.streamlit/secrets.toml`, replace `paste-your-key-here` with a real key from
[aistudio.google.com](https://aistudio.google.com) (free, no credit card), and reload the app.
The sidebar then reads **Gemini key configured**. `GEMINI_MODEL` in the same file sets which
model the app starts on.

An environment variable named `GEMINI_API_KEY` works too and takes over when there is no
secrets file — handy for a server, or for `live_check.py`.

**The key never enters the source or a repository.** `.streamlit/secrets.toml` is gitignored,
and the untouched placeholder is treated as *not configured* so a fresh clone tells you what to
do instead of failing with a confusing API error. Deploying to Streamlit Cloud? Paste the key
into that app's **Secrets** panel under the same name — never into the repo.

Two things worth knowing before you share the app:

- **Everyone using it spends your quota**, because they are all on your key. Fine for a clinic;
  think twice before a public URL.
- **If a key ever leaks** — a screenshot, a chat message, a commit — revoke it in AI Studio and
  issue a new one. It takes seconds and old keys stay live until you do.

---

## HC FORMAT — what gets applied

| # | Rule | Implementation |
|---|------|----------------|
| 1 | Arial, 12 pt, black only | set on every run *and* on the `Normal` + `List Bullet` styles, including `w:cs` / `w:eastAsia` so symbols like `×` and `°` stay Arial |
| 2 | Normal 1 inch margins, professional spacing | 1″ all round, **1.5 line spacing**, tuned space-before/after per block type |
| 3 | Report title (first line **only**): centred, **bold**, underlined, UPPERCASE | `Block(kind="title")` |
| 4 | Main headings: left, **bold**, underlined, UPPERCASE | `Block(kind="heading")` |
| 5 | Findings: every finding a bullet; organ subheadings *italic* + underlined, **never bold** | `Block(kind="bullet")` / `Block(kind="subheading")` |
| 6 | Impression: every point a bullet, every bullet **bold** | `Block(kind="bold_bullet")` |
| 7 | No changes, no summaries, no paraphrase, no omissions, no altered measurements | rule-based parser + `verify.audit` |
| 8 | Delivered as `.docx` | `hc_format.build_docx` |

**Headings recognised:** PATIENT NAME, AGE/SEX, EXAMINATION, CLINICAL HISTORY, TECHNIQUE,
IMAGING SEQUENCES USED, OBSERVATIONS, FINDINGS, IMPRESSION, CONCLUSION, OPINION, COMMENT,
RECOMMENDATION, ADVICE, plus REFERRED BY / DATE / COMPARISON / PROTOCOL variants.
Add your own in `MAIN_HEADINGS` at the top of `hc_format.py`.

**Organ subheadings** (Liver, Gallbladder, Pancreas, Biliary Tree, Kidneys, Cervical Spine,
Muscles, …) are detected by shape rather than by a fixed list, so any organ works. Both
`Liver:` on its own line and `Liver: normal in size…` on one line are handled — the second
becomes an italic underlined label followed by a plain bullet.

---

## The word-loss audit

This is the part that protects you. Rule 7 is the one that gets a radiologist into trouble,
so it is **checked, not assumed**.

After the `.docx` is built, the app reads the file back off disk — not the in-memory
structure, so a rendering bug cannot hide — flattens it to plain text, and compares every
word and number against your original paste, by count.

Two differences are deliberately ignored, and nothing else is:

- **Case**, because rules 3 and 4 force the title and headings to UPPERCASE.
- **Leading list markers** (`-`, `*`, `•`, `1.`, `a)`), because Word draws its own bullet.
  The pattern requires whitespace after the marker, so a measurement that opens a line
  (`2.4 x 1.9 cm lesion…`) is never touched.

A green PASS means every word survived. A red FAIL lists exactly which tokens are missing or
extra, with missing measurements called out separately.

**If you edit in the preview**, the audit does not turn red — it turns amber and becomes a
change-log of your own edits. Correct `14.2` to `15.6` and it reports exactly that, one line
in each column, so you can confirm the only differences from the boss's original are the ones
you made on purpose.

---

## Doctor-wise templates

A template is one doctor's house style — font, size, colour, line spacing, margins, letterhead,
and the bold / italic / underline / uppercase / bullet / alignment / spacing of **every kind of
line**. Pick the doctor in the sidebar and every report comes out in their format; nobody has to
remember a setting.

Manage them in the **Doctor templates** tab: edit the fields, edit the per-line grid, then
**Save**, **Save as new**, or **Delete**. Each one is a plain JSON file in `templates/`, so a
template can be copied between machines or committed to version control.

`HC FORMAT (default)` is the built-in, is exactly the format signed off with the client, and is
read-only — start a doctor's template from it with *Save as new*. The self-test asserts the
built-in has not drifted.

The template drives as-is mode too: even when the text is printed verbatim, the font, size,
spacing and margins still come from the selected doctor.

### Creating and managing them

The sidebar has **➕ New**, **✏️ Edit** and **🗑 Delete** next to the template picker:

- **New** — copy any existing template, name it, name the doctor. It is selected immediately.
- **Edit** — rename, change doctor, font, size, colour, line spacing, all four margins, page
  numbers and letterhead. Renaming moves the file rather than leaving a stale copy behind.
- **Delete** — asks first, and tells you how many style examples go with it. The built-in
  HC FORMAT cannot be edited or deleted.

Per-line formatting and style examples live in the fuller **Doctor templates** tab.

## 🎙 Dictation

The doctor speaks; the app writes. Recording is the browser's own mic widget — nothing to
install — and the audio goes to Gemini, which transcribes it primed with **this doctor's**
vocabulary.

**Just talk.** No dictation commands to memorise — nobody says "full stop" out loud. The model
works the structure out from how you speak:

- punctuation from your phrasing and pauses
- "findings", said and followed by a pause, becomes a `FINDINGS:` heading
- a list cadence becomes a numbered list
- "one point four centimetres" becomes `1.4 cm`

It also behaves like a person taking dictation rather than a speech-to-text engine:

- **filler and false starts are dropped** — "um", "so", a stammered restart; if you abandon a
  sentence and start again, only the finished version is kept
- **spoken corrections are applied** — "the lesion is 4 mm, sorry, 4.2 mm" writes `4.2 mm`;
  "scratch that", "no, make that…" work as you would expect
- **asides are left out** — "can you hear me?", a word to someone in the room — but they are
  reported in the notes so nothing vanishes unseen
- **your English is left alone** — it does not make you more formal or standardise phrasings

If you do happen to say "full stop" or "new paragraph", that still works.

### Choosing who does the listening

Under **Speech engine**:

| Engine | Listening | Layout | Best for |
|---|---|---|---|
| **Gemini** | Gemini | Gemini | Clear English dictation. One pass, nothing to install. |
| **AI4Bharat + Gemini** | AI4Bharat | Gemini | **Indian accents and Hindi/English mixing.** Usually the best setting here. |
| **AI4Bharat only** | AI4Bharat | none | Raw words, no punctuation or structure, no uncertainty flags. Fully offline when run locally. |

[AI4Bharat](https://ai4bharat.org) is IIT Madras's Indic speech work, trained on Indian speech
across **22 scheduled languages plus Indian English**. It hears an Indian doctor considerably
better than a general model — but it returns bare words with no punctuation, no headings and no
sense of what it was unsure about. Gemini is the opposite: weaker on the acoustics, strong at
turning words into a laid-out report and at flagging what it could not make out.

So **AI4Bharat + Gemini** gives each one the half it is actually good at, and the uncertainty
flagging, the `[[brackets]]`, the re-record loop and the vocabulary learning all work exactly
the same.

**Two ways to run AI4Bharat:**

- **Hosted** — a free token from [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)
  in `HF_TOKEN`. Nothing to install; the audio goes to Hugging Face.
- **Local** — `pip install torch transformers`, tick *Run the model on this machine*. A one-time
  download of a gigabyte or so, then it works with **no internet at all**. For identifiable
  patient dictation this is the option to use.

Model IDs are a text field with presets, not a fixed list — AI4Bharat rename and retire Hugging
Face repositories regularly, so if one 404s the app says so plainly and you paste the current ID
rather than waiting for a code change.

Converting the browser's WebM recording for these models needs **ffmpeg** on your PATH
(`pip install pydub` plus an ffmpeg install). Gemini takes the browser audio directly and needs
neither.

### Live, while you speak

A live box shows the words as they come out, so a dead microphone or a mishearing is obvious
immediately rather than after a whole report. There is a level meter and a timer beside it, and
it tells you if it has been hearing nothing. You can type or correct in that box while talking.

Two things run at once: the browser's own speech recognition drives the live preview (instant,
free, no API cost), and the same session is recorded so the **accurate** transcription is done
by Gemini over the audio, primed with this doctor's vocabulary. The rough live text is kept in
an expander so you can compare.

Live preview needs Chrome or Edge. In other browsers recording still works and the accurate
transcription is unaffected — only the as-you-speak preview is missing, and it says so.

### It asks rather than guesses

This is the part that matters in a radiology report. A confidently wrong word is far more
dangerous than a flagged one, so anything not heard cleanly is:

- wrapped in `[[double brackets]]` inline, and
- listed underneath with the reason (noise, ambiguous term, unclear digit, sentence cut off)
  and 2–3 plausible readings.

For each one the doctor can **pick an option**, **type it**, or **say it again** — a second
mic appears for just that phrase, and the model is told it is hearing one short phrase being
repeated, not a report, so it does not try to rebuild sentences around it. If the repeat is
still unclear it says so instead of guessing again.

The transcript box warns while any `[[...]]` remain, so a bracketed guess cannot reach a report
unnoticed. Very poor audio is called out up front — a bad recording is not worth correcting
word by word.

### It learns the doctor's voice

Every fix is remembered as a `heard → said` pair, and the corrected word joins that doctor's
vocabulary. Both are fed to the next transcription:

```
- heard "colic list" -> they said "cholelithiasis"
```

Press **🎓 Learn these words** and the corrected transcript is also mined for medical terms,
drug names and set phrases worth expecting next time. So the second dictation is more accurate
than the first, and the tenth more accurate again. Caps at 300 terms and 60 fixes per doctor.

Everything learned is visible and prunable under *Doctor templates → Dictation vocabulary*.

### Then what

Two ways out, matching what was dictated:

- **Send to Single report** — the words are treated verbatim, HC FORMAT layout, word-loss audit
  applies. Use when the finished report was dictated.
- **Send to Draft in doctor's style** — rewrites into house style. Use when rough notes were
  dictated.

Dictation needs the AI engine and a configured key. It cannot learn on the built-in HC FORMAT,
which holds no doctor's voice — create a doctor template first.

## Do you need a backend?

**Right now, no.** Streamlit *is* the backend — a Python server — and templates are JSON files
beside the code. For one clinic on one machine that is the correct amount of infrastructure,
and adding a database would buy nothing.

Saving is already made safe for that case: writes go to a temp file and are moved into place so
an interrupted save cannot corrupt a template, the previous 10 versions are kept in
`templates/_backups/`, and if a template changed on disk while you had it open the save is
**refused** rather than silently overwriting someone else's work.

**You will need a backend when any of these becomes true:**

| Situation | Why files stop being enough | What to add |
|---|---|---|
| **Deploying to Streamlit Cloud** | The filesystem is wiped on every restart and redeploy. **Every doctor template and everything the app learned would be lost.** | Anything persistent — Postgres, S3, or a mounted volume |
| Two or more people editing templates at once | The conflict check catches a clobber and refuses it, but a busy clinic will hit that refusal often | A database with real transactions |
| More than one machine | Templates live on whichever PC saved them | Shared storage or an API |
| You need to know who generated which report | There is no audit trail and no login | Auth + an events table |
| Patient data must be retained or auditable | Nothing is stored server-side today, which is a feature until a regulator asks for records | Proper storage, encryption at rest, retention policy |

**The one that matters most:** if you deploy this to Streamlit Cloud as it stands, it will work
and then quietly lose every doctor's template on the next restart. Run it on a clinic PC, or
put the templates somewhere persistent first. Committing `templates/*.json` to the repository is
a crude but genuinely workable stopgap for a small team.

**A realistic path:** clinic PC now → shared network folder if a second machine appears →
SQLite or Postgres only when several people edit concurrently or you need an audit trail. Do not
build the database before the second machine exists.

## AI drafting in a doctor's own style

> **This is the one tool in the app that writes words.** Everywhere else the report is
> reproduced verbatim and the audit proves it. Here an AI rewrites rough notes into the
> doctor's usual phrasing, so the wording changes on purpose. It is for a doctor drafting
> their **own** report — never for a report someone else sent you.

The doctor types shorthand:

```
mult gallstones 4-11mm, no chole
liver 14.2cm normal, no SOL
kidneys nad
```

…and gets it back in their own voice, because the app sends their previously signed reports
as few-shot examples. Choose whether to rewrite the IMPRESSION only, the FINDINGS only, or
the whole report — narrower is safer, and the impression is where house style shows most.

### The loop

Four steps, and step 4 is the point — every correction teaches the next report.

**1 · Notes in.** Shorthand, unordered, however the doctor thinks.

**2 · The AI asks instead of guessing.** Where the notes are genuinely ambiguous — a missing
laterality, a measurement with no organ attached, shorthand it cannot map — it comes back with
a question and 2–4 concrete options, each one a phrase that could go straight into the report.
The doctor picks one or types their own. Anything they skip becomes a listed *assumption*
rather than a silent guess. **Remember these answers** saves them on the template so the same
question is never asked twice. Zero questions is common and fine; it only asks when the answer
would change the report.

**3 · The doctor corrects the draft.** Notes and draft sit side by side, draft editable.

**4 · The app learns.** Press **🎓 Learn from my corrections** and the before/after pair goes to
the model, which distils *reusable* rules — "Write 'calculi' rather than 'stones'", "Number the
impression points; do not use bullets" — not facts about this one report. Those rules are saved
on the doctor's template and injected into every future prompt, where they **override** anything
inferred from the examples. One optional line of "why did you change it?" makes the lesson much
sharper. If the edit teaches nothing generalisable, the raw before/after pair is still stored and
still shown to the next draft.

Four kinds of evidence go into each prompt, strongest first: rules from corrections → the
doctor's own style notes → before/after pairs → whole signed reports.

**Inspect and prune what it learned.** *Doctor templates* shows every learned rule with a
checkbox — untick to make the AI forget it — plus the full correction history and the list of
questions already answered. A wrong lesson is one click from gone. Nothing is a black box.

**Caps.** 8 reports and 12 corrections per doctor, newest kept. Few-shot prompts stop improving
well before that, and an unbounded prompt gets slow and expensive.

### When the report is finished

Two options, and they differ in whose environment they teach:

1. **Save to <doctor>** — adds the report to their voice samples and keeps every rule learned
   above. For a doctor who already has a template.
2. **Create a doctor from this report** — builds a fresh template (formatting, letterhead and
   voice) seeded with this report, so their very next draft already sounds like them. For the
   first time a new doctor's work comes through. A new doctor inherits formatting from the
   template you base it on, never another doctor's voice.

**Seeding by hand.** You can also add past reports under *Doctor templates → Style examples*, or
format a report in the Single report tab and press **📚 Save this report as a style example**.
Two or three finished reports is usually enough. With nothing on file the app says so plainly
and the AI stays close to your notes rather than inventing a style.

Everything learned here affects **only** this tab. It never touches normal formatting.

**Guardrails**

- The prompt forbids inventing or dropping a finding, and requires every number and unit to
  come through unchanged.
- Your notes and the draft sit side by side so you read exactly what changed.
- A deterministic check compares every number in your notes against the draft and flags any
  that are missing or altered — `liver 14.2 cm` rewritten as `15.6 cm` is caught without a
  second API call. It checks measurements only; it cannot tell you a finding was reworded,
  which is your read.
- **➡️ Send to Single report** hands the approved draft back to the normal pipeline, where the
  text is treated verbatim and the word-loss audit applies again.

## Rich text in the preview

Select any words in the editable preview and a small toolbar appears: **B**, *I*, <u>U</u>
(or Ctrl+B / Ctrl+I / Ctrl+U). Useful in IMPRESSION, where a doctor writing the report wants
one phrase emphasised inside a point.

Inline emphasis **adds to** the line's format rather than replacing it. Italicise a word inside
an IMPRESSION bullet and you get bold + italic — rule 6's bold is never lost. The emphasis is
stored with the text, so it survives editing, reclassifying the line, and the round-trip into
Word as separate runs.

Because the runs reassemble to exactly the same characters, the word-loss audit is unaffected:
formatting a word is not changing it, and the audit still reads PASS.

## As-is mode

Tick **Print exactly as pasted** and the app adds nothing and removes nothing:

- original line breaks and **blank lines** kept
- leading **indentation** kept (`xml:space="preserve"` in the Word XML) and tabs stay real tabs
- list markers like `1.` or `-` **not** stripped, because no Word bullet replaces them
- nothing upper-cased, no heading detection, no bullets

Use it when the text already looks the way it should print — pasted out of another system, or
typed by the doctor. Font, size, line spacing and margins still come from the template, so the
page still looks like that doctor's paper.

The audit adapts: in as-is mode it stops ignoring leading list markers, because the engine kept
them, so they must still be counted on both sides.

## Features

- **Paste or upload** — `.txt`, `.md`, `.docx`, `.pdf`. PDFs with a text layer are read
  locally by `pypdf`; scans and photos (`.png`, `.jpg`) go through AI OCR, and the
  transcription is shown in an editable box before anything is generated.
- **Edit directly in the live preview** — the preview *is* the editor. It renders the page
  exactly as Word will (centred bold underlined title, bold underlined headings, italic
  underlined organ subheadings, bulleted findings, bold impression bullets) and every line
  is editable in place. Click a line, fix the typo, retype the measurement. **Enter** starts
  a new line in the same format, **Backspace** on an empty line deletes it, and the **¶**
  button in the left gutter changes a line's format when detection guessed wrong — a finding
  that should have been an organ subheading, a line that should have been an impression
  bullet. The `.docx` and the audit follow every edit. **Reset** discards your edits and
  re-detects; edits also reset by themselves whenever the source text or a parsing option
  changes, so you can never be editing a structure that no longer exists.
- **Batch mode** — upload many files, or paste many reports separated by a line of `---`,
  and get one ZIP back plus a per-report audit table.
- **Letterhead** — optional clinic logo, name, address and contact, centred above the title
  with a rule beneath. Letterhead words are excluded from the audit so they don't read as
  drift.
- **Page numbers** — optional `Page X of Y` footer using real Word fields.
- **PDF export** — needs Microsoft Word installed plus `pip install docx2pdf`. The `.docx`
  is unaffected if it isn't available.

---

## Files

```
app.py         Streamlit UI: input, editable preview, audit, batch, ZIP, template manager
editor/        the in-place WYSIWYG editor + rich-text toolbar — a self-contained Streamlit component (plain HTML/JS, no npm, no build step)
live_dictate/  the live dictation component: browser speech recognition for the as-you-speak preview, MediaRecorder for the audio Gemini transcribes
templates.py   doctor-wise templates: the Template/BlockStyle model, the built-in HC FORMAT, JSON load/save, and the learning store (corrections, distilled rules, answered questions)
templates/     one JSON file per doctor — formatting AND everything learned about their voice (created by the app; empty until you add one)
hc_format.py   parse_report() structure detection + build_docx() styling, driven by a Template
verify.py      the word-loss audit
ai_parser.py   optional Gemini: layout classifier, OCR, dictation transcription and layout, few-shot house-style drafting, measurement guard, model listing
speech.py      speech-to-text backends: AI4Bharat via Hugging Face (hosted or local), audio conversion
readers.py     txt / docx / pdf input readers
corpus.py      the clinic library: offline term extraction from uploaded books/papers into a shared vocabulary corpus that biases STT and AI prompts (suggest-only)
selftest.py           offline: asserts every HC FORMAT rule on the real .docx XML, plus every feature in isolation
integration_check.py  offline: the journeys a clinic actually takes, end to end, and the handoffs between features
security_check.py     offline: adversarial input — traversal, collisions, corrupt files, injected model IDs, prompt injection, DoS, leaked secrets
live_check.py         online: the AI features (needs GEMINI_API_KEY in the environment)
secrets.toml.example  copy to .streamlit/secrets.toml and put your key in it
samples/       example reports
output/        selftest results
```

Run all three offline suites any time you change anything:

```bash
python selftest.py && python integration_check.py && python security_check.py
```

None of them need a key or a network.

It parses each sample, builds the `.docx`, then re-opens it and asserts the actual Word XML:
Arial 12 pt black on every run, 1″ margins, the title centred/bold/underlined/uppercase,
headings bold+underlined+uppercase, findings subheadings italic+underlined **and not bold**,
impression bullets bold — and finally that the word-loss audit passes.

---

## Deploying so your boss can use it too

Free, on [Streamlit Community Cloud](https://share.streamlit.io):

1. Push this folder to a GitHub repository.
2. Sign in at share.streamlit.io and point it at the repo, main file `app.py`.
3. Deploy. You get a public URL — no Python needed on anyone else's machine.

The rule-based engine runs fine there with no secrets configured. If you want the AI engine
available on the hosted app, each user pastes their own free key into the sidebar; it is
never stored.

**Before hosting patient data anywhere**, check your clinic's privacy obligations. The
rule-based engine is fully offline on your own machine and sends nothing anywhere — that is
the safest option for identifiable reports, and it is the default for exactly that reason.
