"""
Optional Gemini-assisted structuring.

The AI is used *only as a classifier*: it decides which line is a title, a main
heading, an organ subheading, a finding bullet or an impression bullet.  It is
forbidden to rewrite text, and app.py re-runs the word-loss audit on whatever
it returns - if a single word drifts, the deterministic parser in hc_format.py
wins.  That keeps the "not a single word lost" guarantee intact even when the
AI is switched on for messy, unstructured input.

Requires a free API key from https://aistudio.google.com (no credit card).
"""

from __future__ import annotations

import json
from typing import Any

from hc_format import Block

VALID_KINDS = {"title", "heading", "subheading", "bullet", "bold_bullet", "text"}

SYSTEM_PROMPT = """You are a radiology report LAYOUT CLASSIFIER, not a writer.

You receive the raw text of a radiology report. You return JSON only.

ABSOLUTE RULES - violating any of these makes the output useless:
1. NEVER change, rewrite, rephrase, translate, correct, expand or shorten any word.
2. NEVER omit any sentence, clause, number, unit or measurement.
3. NEVER add clinical words of your own. No summaries. No interpretations.
4. Copy every character of the source into some block's "text", in source order.
   The only characters you may drop are leading list markers ("-", "*", "1.", bullets).
   The only change you may make is upper-casing the title and the main headings.

Classify every line into one of these kinds:
- "title"        : the very first line, the name of the report. Output UPPERCASE. Exactly one.
- "heading"      : a main section label - PATIENT NAME, AGE/SEX, EXAMINATION,
                   CLINICAL HISTORY, TECHNIQUE, FINDINGS, IMPRESSION, COMMENT,
                   RECOMMENDATION, CONCLUSION, OBSERVATIONS, etc. Output UPPERCASE.
                   If the source wrote content on the same line as the heading,
                   emit the heading block first and the content as a following block.
- "subheading"   : an organ / region label inside FINDINGS - Liver, Gallbladder,
                   Pancreas, Biliary Tree, Kidneys, Cervical Spine, Muscles, etc.
                   Keep the original casing. Keep a trailing colon if present.
- "bullet"       : a finding. One source sentence-group per bullet, verbatim.
- "bold_bullet"  : a point under IMPRESSION or CONCLUSION, verbatim.
- "text"         : anything else - patient details, technique prose, "Please
                   correlate clinically", the referral thanks, doctor name and
                   degrees, disclaimers.

Return exactly this JSON shape and nothing else:
{"blocks": [{"kind": "title", "text": "..."}, {"kind": "heading", "text": "..."}]}
"""


DRAFT_PROMPT = """You rewrite a radiologist's rough notes into their own house style.

You will be shown examples of reports THIS radiologist has already signed. Learn from
them: sentence shape, preferred terminology, how findings are ordered, how measurements
are phrased, how impressions are numbered and worded, the level of detail, the closing
lines.

RULES
1. Keep every clinical fact from the notes. Never invent a finding, a measurement, a
   laterality, a date or a diagnosis that is not in the notes.
2. Never drop a clinical fact from the notes, however roughly it is written.
3. Keep every number, unit and measurement EXACTLY as given. 14.2 cm stays 14.2 cm.
4. Expand shorthand into the radiologist's usual full phrasing (for example "no SOL" ->
   whatever wording their examples use). Do not add new observations while doing so.
5. If something in the notes is ambiguous, keep the radiologist's own words rather than
   guessing.
6. Match the section headings the examples use. Return plain text only, no markdown.

You are drafting, not diagnosing. The radiologist reads and approves every word before
it is signed."""


def build_draft_prompt(
    template,
    raw_notes: str,
    section: str = "the whole report",
    answers: dict[str, str] | None = None,
) -> str:
    """
    Assemble the few-shot prompt. Pure text, no network - so it can be tested.

    Four kinds of evidence go in, in order of how strongly they bind:
      1. rules distilled from the doctor's own corrections  (strongest)
      2. the doctor's free-text house-style notes
      3. before/after pairs from past drafts
      4. whole reports the doctor has signed
    """
    parts: list[str] = []

    doctor = (template.doctor or template.name or "this radiologist").strip()
    parts.append(f"Radiologist: {doctor}")

    preferences = [p.strip() for p in (getattr(template, "preferences", None) or []) if p.strip()]
    if preferences:
        parts.append(
            f"\nRules learned from {doctor}'s own corrections. These override anything "
            "you infer from the examples:"
        )
        parts.extend(f"- {rule}" for rule in preferences)

    if (template.style_notes or "").strip():
        parts.append(f"\nHouse-style notes from {doctor}:\n{template.style_notes.strip()}")

    corrections = [
        c for c in (getattr(template, "corrections", None) or [])
        if (c.before or "").strip() and (c.after or "").strip()
    ][-4:]
    if corrections:
        parts.append(
            f"\nPast drafts and how {doctor} corrected them. Write like the AFTER, not the BEFORE:"
        )
        for i, c in enumerate(corrections, start=1):
            parts.append(f"\n--- CORRECTION {i} ---\nBEFORE:\n{c.before}\nAFTER:\n{c.after}")
            if (c.note or "").strip():
                parts.append(f"REASON GIVEN: {c.note.strip()}")

    examples = [e.strip() for e in (template.examples or []) if e.strip()]
    if examples:
        parts.append(f"\nReports previously signed by {doctor} - learn the style from these:")
        for i, example in enumerate(examples, start=1):
            parts.append(f"\n--- EXAMPLE {i} ---\n{example}")
    elif not preferences and not corrections:
        parts.append(
            "\nNothing is on file for this radiologist yet, so keep the wording close to the "
            "notes and use standard academic radiology phrasing."
        )

    standing = {**(getattr(template, "answered", None) or {}), **(answers or {})}
    if standing:
        parts.append("\nAnswers this radiologist has already given - do not ask these again:")
        parts.extend(f"- {q} -> {a}" for q, a in standing.items())

    # The scope must be an ORDER, not a parenthetical label - given only the
    # label, a model handed three lines of shorthand happily returns an
    # impression when the whole report was asked for.
    scope = (section or "").lower()
    if "whole report" in scope:
        parts.append(
            "\nSCOPE - THE WHOLE REPORT. Produce a complete structured report:\n"
            "- a study title line (infer the study from the notes, e.g. USG ABDOMEN REPORT)\n"
            "- FINDINGS: with every fact from the notes stated organ by organ\n"
            "- IMPRESSION: summarising the abnormal findings\n"
            "Never return an impression alone - the findings section is mandatory."
        )
    elif "findings" in scope:
        parts.append(
            "\nSCOPE - FINDINGS ONLY. Produce only the FINDINGS section, organ "
            "by organ. No title, no impression."
        )
    elif "impression" in scope:
        parts.append(
            "\nSCOPE - IMPRESSION ONLY. Produce only the IMPRESSION section: "
            "2-4 concise points. No findings section."
        )
    elif "shorthand" in scope:
        parts.append(
            "\nSCOPE - SHORTHAND EXPANSION ONLY. Expand abbreviations and "
            "shorthand into full words and nothing else. Keep the author's "
            "wording, line order and structure exactly as written; do not "
            "restyle, do not reorganise, do not add headings that are not there."
        )

    parts.append(
        f"\n--- ROUGH NOTES TO REWRITE ({section}) ---\n{raw_notes.strip()}"
        f"\n\n--- {doctor.upper()}'S VERSION ---"
    )
    return "\n".join(parts)


ASK_PROMPT = """You rewrite a radiologist's rough notes into their own house style, and you
ASK when you are not sure.

Everything in DRAFT_PROMPT applies. In addition:

Raise a question whenever the notes are genuinely ambiguous and the answer would change the
report - a missing laterality, a measurement with no organ attached, shorthand you cannot
map with confidence, a finding that could belong in FINDINGS or in IMPRESSION, a house-style
choice the examples do not settle.

Do NOT ask about anything the learned rules, the corrections or the previous answers already
settle. Do not ask cosmetic questions. Three questions is a lot; zero is fine and common.

For each question give 2-4 concrete options, each one a phrase that could go straight into
the report. The radiologist can always type their own answer instead.

Meanwhile produce your best draft, using your most likely reading of each ambiguity, and list
those readings in "assumptions".

Return JSON, nothing else:
{
  "draft": "the full rewritten text",
  "questions": [
    {"id": "q1",
     "question": "The notes say 'kidney 9.8 cm' - which side?",
     "why": "Laterality changes the finding and is not in the notes.",
     "options": ["Right kidney", "Left kidney", "Both kidneys"]}
  ],
  "assumptions": ["Read 'no chole' as no evidence of acute cholecystitis."]
}"""


def draft_with_questions(
    raw_notes: str,
    template,
    api_key: str,
    model: str,
    *,
    section: str = "the whole report",
    answers: dict[str, str] | None = None,
    temperature: float = 0.2,
) -> dict:
    """Draft, and come back with clarifying questions where the notes are ambiguous."""
    import json as _json

    from google.genai import types

    client = _client(api_key)
    response = _generate(client,
        model=model,
        contents=[build_draft_prompt(template, raw_notes, section, answers)],
        config=types.GenerateContentConfig(
            system_instruction=DRAFT_PROMPT + "\n\n" + ASK_PROMPT,
            response_mime_type="application/json",
            temperature=temperature,
        ),
    )

    data = _json.loads(response.text or "{}")
    questions = []
    for q in data.get("questions") or []:
        if not isinstance(q, dict) or not str(q.get("question", "")).strip():
            continue
        questions.append(
            {
                "id": str(q.get("id") or f"q{len(questions) + 1}"),
                "question": str(q["question"]).strip(),
                "why": str(q.get("why") or "").strip(),
                "options": [str(o).strip() for o in (q.get("options") or []) if str(o).strip()],
            }
        )
    draft = str(data.get("draft") or "").strip()

    # The negation tripwire. A rough-notes negative that the model asserted is
    # a patient-safety event, not a style problem - stop hard, never return
    # the draft. (negation.NegationMismatchException)
    import negation

    negation.assert_polarity(raw_notes, draft)

    return {
        "draft": draft,
        "questions": questions,
        "assumptions": [str(a).strip() for a in (data.get("assumptions") or []) if str(a).strip()],
    }


DISTILL_PROMPT = """You compare a draft with the radiologist's corrected version and work out
what they want in general.

Return short, imperative, reusable style rules - the kind that would apply to the NEXT report,
not facts about this one.

GOOD: "Write 'calculi' rather than 'stones'."
GOOD: "Number the impression points; do not use bullets."
GOOD: "State the craniocaudal span for the liver even when normal."
BAD:  "The liver measured 14.2 cm."          (a fact about this report)
BAD:  "Be more accurate."                     (not actionable)

Ignore differences that are pure typo fixes or one-off clinical content. If the correction
teaches nothing generalisable, return an empty list. Two or three rules is plenty.

Return JSON only: {"rules": ["...", "..."]}"""


def distill_preferences(
    before: str, after: str, template, api_key: str, model: str, note: str = ""
) -> list[str]:
    """Turn one correction into reusable style rules. Returns [] when it teaches nothing."""
    import json as _json

    from google.genai import types

    doctor = (template.doctor or template.name or "the radiologist").strip()
    existing = [p for p in (getattr(template, "preferences", None) or [])]
    body = [f"Radiologist: {doctor}"]
    if existing:
        body.append(
            "Rules already known - do not repeat these, only add genuinely new ones:\n"
            + "\n".join(f"- {r}" for r in existing)
        )
    if note.strip():
        body.append(f"The radiologist's own comment on this edit: {note.strip()}")
    body.append(f"--- DRAFT ---\n{before}")
    body.append(f"--- {doctor.upper()}'S CORRECTED VERSION ---\n{after}")

    client = _client(api_key)
    response = _generate(client,
        model=model,
        contents=["\n\n".join(body)],
        config=types.GenerateContentConfig(
            system_instruction=DISTILL_PROMPT,
            response_mime_type="application/json",
            temperature=0.0,
        ),
    )
    data = _json.loads(response.text or "{}")
    return [str(r).strip() for r in (data.get("rules") or []) if str(r).strip()]


def missing_facts(raw_notes: str, drafted: str) -> list[str]:
    """
    Numbers and measurements present in the notes but absent from the draft.

    Cheap, deterministic safety net over rule 3 - it cannot catch a reworded
    finding, but a dropped or altered measurement is exactly the failure that
    matters most, and this catches it without another API call.
    """
    import re

    pattern = re.compile(r"\d+(?:\.\d+)?")
    note_numbers = pattern.findall(raw_notes)
    draft_numbers = pattern.findall(drafted)
    out = []
    for number in note_numbers:
        if draft_numbers.count(number) < note_numbers.count(number) - out.count(number):
            out.append(number)
    return sorted(set(out))


TRANSCRIBE_PROMPT = """You are an experienced medical scribe taking dictation from a
radiologist. Work like a person who has sat beside this doctor for years, not like a
literal speech-to-text engine.

The doctor speaks NORMALLY. They will not say "full stop" or "new paragraph", and they
should not have to. Work the structure out yourself from how they speak:

- Punctuate from their phrasing, pauses and intonation. Sentences end where they end.
- A dictated section name becomes a heading on its own line: they say "findings" and pause,
  you write "FINDINGS:".
- A run of separate observations becomes separate lines, one per finding.
- "number one ... number two", or a clear list cadence, becomes a numbered list.
- Convert spoken numbers to figures with their units: "one point four centimetres" -> "1.4 cm",
  "nine point eight by four point four" -> "9.8 x 4.4 cm" when the unit is clear from context.
- If they DO happen to say "full stop", "comma", "new line", "new paragraph" or "next point",
  treat it as the punctuation they mean rather than printing the words.

Behave like a person listening, which means:

- Drop filler and false starts: "um", "uh", "so", "let me see", a stammered restart. If they
  begin a sentence, abandon it and start again, keep only the version they finished.
- Honour spoken self-corrections. "the lesion is 4 mm, sorry, 4.2 mm" -> "4.2 mm".
  "scratch that", "no, make that...", "correction..." -> apply the change silently and keep
  only the corrected text.
- Understand asides that are not part of the report ("can you hear me?", talking to someone
  in the room) and leave them out - but mention them in "notes" so nothing vanishes unseen.
- Keep the doctor's own vocabulary and sentence shape. Do not upgrade their English, do not
  make it more formal, do not standardise their preferred phrasings.

NEVER do these:
- Never add a finding, a measurement, a laterality or a negation that was not spoken.
- Never drop a clinical statement they actually made.
- Never guess a digit. A wrong measurement is the worst failure this system can produce.
- Never summarise.

WHEN YOU ARE NOT SURE - THIS MATTERS MOST
Never invent a word to fill a gap. If speech was unclear, drowned by noise, clipped, or could
be two different medical terms, put your best guess in the transcript wrapped in [[...]] and
add an entry to "unclear". A flagged guess is useful; a confident wrong word is dangerous.
Flag it whenever any of these is true:
  - you could not make out the audio
  - it could plausibly be another term, especially a drug, an organ or a laterality
  - a digit or a unit was not crisp
  - the sentence was cut off

Be honest rather than tidy. Two flags on a noisy recording is a good outcome.

Return JSON only:
{
  "transcript": "the full text, punctuated and laid out, with [[uncertain bits]] marked inline",
  "unclear": [
    {"id": "u1",
     "heard": "the exact text as it appears in the transcript, without the brackets",
     "reason": "why you are unsure - noise, ambiguous term, unclear digit, cut off",
     "options": ["most likely reading", "other plausible reading"]}
  ],
  "audio_quality": "good | noisy | very poor",
  "notes": "anything the radiologist should know - an aside you left out, a cut-off ending, background speech - or empty"
}"""


def build_transcribe_prompt(template, context: str = "") -> str:
    """
    Prime the transcriber with this doctor's vocabulary. Pure text, no network.

    Two things move accuracy most: the terms they actually say, and the words
    that have been misheard for them before.
    """
    doctor = (template.doctor or template.name or "this radiologist").strip()
    parts = [f"You are transcribing {doctor} dictating a radiology report."]

    vocabulary = [t.strip() for t in (getattr(template, "vocabulary", None) or []) if t.strip()]
    if vocabulary:
        parts.append(
            "Terms this radiologist actually uses. Prefer these spellings when what you hear "
            "is close to one of them:\n" + ", ".join(vocabulary)
        )

    fixes = [
        c for c in (getattr(template, "dictation_fixes", None) or [])
        if (c.before or "").strip() and (c.after or "").strip()
    ][-25:]
    if fixes:
        parts.append(
            "Words that have been misheard for this radiologist before. When the audio is "
            "close to the left-hand side, they almost certainly said the right-hand side:\n"
            + "\n".join(f'- heard "{c.before}" -> they said "{c.after}"' for c in fixes)
        )

    examples = [e.strip() for e in (template.examples or []) if e.strip()][-2:]
    if examples:
        parts.append(
            "Reports this radiologist has signed, for vocabulary and sentence shape only. "
            "Do NOT copy their content into the transcript:\n\n"
            + "\n\n".join(examples)
        )

    if context.strip():
        parts.append(f"The radiologist says this recording is about: {context.strip()}")

    if not vocabulary and not fixes and not examples:
        parts.append(
            "Nothing is on file for this radiologist yet, so transcribe conservatively and "
            "flag anything you are unsure of rather than guessing."
        )

    return "\n\n".join(parts)


def transcribe_dictation(
    audio_bytes: bytes,
    mime_type: str,
    template,
    api_key: str,
    model: str,
    *,
    context: str = "",
) -> dict:
    """Transcribe a dictation, flagging every bit it could not hear cleanly."""
    import json as _json

    from google.genai import types

    client = _client(api_key)
    response = _generate(client,
        model=model,
        contents=[
            types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
            build_transcribe_prompt(template, context),
        ],
        config=types.GenerateContentConfig(
            system_instruction=TRANSCRIBE_PROMPT,
            response_mime_type="application/json",
            temperature=0.0,
        ),
    )

    data = _json.loads(response.text or "{}")
    unclear = []
    for u in data.get("unclear") or []:
        if not isinstance(u, dict) or not str(u.get("heard", "")).strip():
            continue
        unclear.append(
            {
                "id": str(u.get("id") or f"u{len(unclear) + 1}"),
                "heard": str(u["heard"]).strip(),
                "reason": str(u.get("reason") or "").strip(),
                "options": [str(o).strip() for o in (u.get("options") or []) if str(o).strip()],
            }
        )
    return {
        "transcript": str(data.get("transcript") or "").strip(),
        "unclear": unclear,
        "audio_quality": str(data.get("audio_quality") or "").strip(),
        "notes": str(data.get("notes") or "").strip(),
    }


STRUCTURE_DICTATION_PROMPT = """You are a medical scribe tidying the raw output of a speech
recogniser into a radiology report.

You are NOT listening to audio. You are reading bare text from an ASR system, so it has no
punctuation, no capitals, no paragraphs, and it may contain recognition errors.

Do exactly what a human scribe does with a rough transcript:

- Punctuate and paragraph it from the sense of the words.
- Turn a spoken section name into a heading on its own line: "findings" -> "FINDINGS:".
- Turn a list cadence into a numbered list.
- Convert spoken numbers to figures with units: "one point four centimetres" -> "1.4 cm".
- Drop filler and false starts. If a sentence was abandoned and restarted, keep the finished one.
- Apply spoken self-corrections: "4 mm sorry 4.2 mm" -> "4.2 mm"; "scratch that"; "make that".
- Leave out asides that are not part of the report, and say so in "notes".
- If the doctor was speaking an Indian language, keep the report in THAT language. Do not
  translate. Standard radiological terms stay in their usual form.

NEVER add a finding, a measurement, a laterality or a negation that is not in the raw text.
NEVER drop a clinical statement. NEVER summarise. NEVER invent a digit.

The ASR will have made mistakes. Where a word is clearly garbled, or is a medical term the
recogniser has plainly mangled, or a number looks wrong or impossible, put your best reading in
[[double brackets]] and list it in "unclear" with alternatives. Do not silently repair it - a
confident wrong measurement is the most dangerous thing this system can produce.

Return JSON only, same shape as an audio transcription:
{
  "transcript": "the laid-out text, with [[uncertain bits]] marked inline",
  "unclear": [
    {"id": "u1", "heard": "text exactly as it appears without brackets",
     "reason": "why", "options": ["most likely", "alternative"]}
  ],
  "audio_quality": "good",
  "notes": "anything the radiologist should know, or empty"
}"""


def structure_dictation(
    raw_text: str,
    template,
    api_key: str,
    model: str,
    *,
    context: str = "",
    language: str = "",
) -> dict:
    """
    Lay out bare ASR text as a report.

    Used when the listening was done by AI4Bharat, which returns accurate words
    but no punctuation or structure. Same return shape as transcribe_dictation,
    so the tab handles both identically.
    """
    import json as _json

    from google.genai import types

    body = [build_transcribe_prompt(template, context)]
    if language:
        body.append(f"The doctor was speaking: {language}. Keep the report in that language.")
    body.append(f"--- RAW SPEECH RECOGNISER OUTPUT ---\n{raw_text.strip()}")

    client = _client(api_key)
    response = _generate(client,
        model=model,
        contents=["\n\n".join(body)],
        config=types.GenerateContentConfig(
            system_instruction=STRUCTURE_DICTATION_PROMPT,
            response_mime_type="application/json",
            temperature=0.0,
        ),
    )

    data = _json.loads(response.text or "{}")
    unclear = []
    for u in data.get("unclear") or []:
        if not isinstance(u, dict) or not str(u.get("heard", "")).strip():
            continue
        unclear.append(
            {
                "id": str(u.get("id") or f"u{len(unclear) + 1}"),
                "heard": str(u["heard"]).strip(),
                "reason": str(u.get("reason") or "").strip(),
                "options": [str(o).strip() for o in (u.get("options") or []) if str(o).strip()],
            }
        )
    return {
        "transcript": str(data.get("transcript") or "").strip(),
        "unclear": unclear,
        "audio_quality": str(data.get("audio_quality") or "good").strip(),
        "notes": str(data.get("notes") or "").strip(),
    }


def transcribe_repeat(
    audio_bytes: bytes,
    mime_type: str,
    template,
    api_key: str,
    model: str,
    *,
    heard: str,
    reason: str = "",
) -> str:
    """
    Transcribe a short re-recording of one phrase the doctor was asked to repeat.

    Scoped deliberately: the model is told this is one phrase, not a report, so
    it does not try to reconstruct sentences around it.
    """
    from google.genai import types

    client = _client(api_key)
    instruction = (
        "The radiologist is repeating ONE short phrase that was not heard clearly the first "
        f'time. The unclear attempt was transcribed as: "{heard}".'
        + (f" The problem was: {reason}." if reason else "")
        + " Return ONLY the words in this new recording, as plain text. No JSON, no quotes, "
        "no commentary, no punctuation you did not hear dictated. If it is still not clear, "
        "return exactly: UNCLEAR"
    )
    response = _generate(client,
        model=model,
        contents=[
            types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
            build_transcribe_prompt(template) + "\n\n" + instruction,
        ],
        config=types.GenerateContentConfig(temperature=0.0),
    )
    return (response.text or "").strip()


REVIEW_PROMPT = """You proofread a speech-to-text transcript of a radiology report.

You are given the transcript and the list of terms this radiologist actually uses.
Find words the recogniser plainly got wrong - where what it wrote is not a real
phrase in this context, and one of the radiologist's known terms is what they
almost certainly said.

The classic case: "colic list" for "cholelithiasis". No spelling comparison
catches that, because the letters barely overlap. You catch it because you know
what a gallbladder finding sounds like and what the surrounding sentence means.

RULES
- Only flag something you are genuinely confident about. Two good suggestions
  beat ten guesses; a radiologist who stops trusting these will ignore all of them.
- Only suggest terms from the radiologist's list, or standard radiological terms
  that obviously fit. Never invent a finding.
- Never suggest a change to a number, a measurement, a unit or a laterality.
  Those are for the radiologist alone - a wrong measurement is the most
  dangerous thing this system can produce.
- If the transcript reads correctly, return an empty list. That is a normal answer.

Return JSON only:
{"suggestions": [
  {"heard": "exact text as it appears in the transcript",
   "suggested": "what they almost certainly said",
   "why": "one short clause"}
]}"""


def review_transcript(
    transcript: str, template, api_key: str, model: str
) -> list[dict]:
    """
    Ask the model to find mis-transcriptions the rules cannot.

    Edit distance and phonetics catch a split word or a near-miss spelling.
    They cannot catch "colic list" for "cholelithiasis" - the letters and the
    sounds are both too far apart. Meaning is what bridges that, so this pass
    reads the whole sentence with the doctor's vocabulary in hand.

    Suggestions only. Nothing is applied without the radiologist.
    """
    import json as _json

    from google.genai import types

    vocabulary = [v for v in (getattr(template, "vocabulary", None) or []) if v.strip()]
    fixes = [
        f'"{c.before}" -> "{c.after}"'
        for c in (getattr(template, "dictation_fixes", None) or [])
        if (c.before or "").strip() and (c.after or "").strip()
    ][-20:]

    body = [f"TRANSCRIPT:\n{transcript.strip()}"]
    if vocabulary:
        body.append("Terms this radiologist uses:\n" + ", ".join(vocabulary))
    if fixes:
        body.append("Mistakes made for this radiologist before:\n" + "\n".join(fixes))

    client = _client(api_key)
    response = _generate(client,
        model=model,
        contents=["\n\n".join(body)],
        config=types.GenerateContentConfig(
            system_instruction=REVIEW_PROMPT,
            response_mime_type="application/json",
            temperature=0.0,
        ),
    )

    data = _json.loads(response.text or "{}")
    out = []
    for item in data.get("suggestions") or []:
        if not isinstance(item, dict):
            continue
        heard = str(item.get("heard") or "").strip()
        suggested = str(item.get("suggested") or "").strip()
        # A suggestion that is not actually in the transcript cannot be applied,
        # and one that changes a number is refused on principle.
        if not heard or not suggested or heard not in transcript:
            continue
        if any(ch.isdigit() for ch in heard) or any(ch.isdigit() for ch in suggested):
            continue
        out.append({"heard": heard, "suggested": suggested,
                    "why": str(item.get("why") or "").strip()})
    return out


def distill_vocabulary(
    heard: str, corrected: str, template, api_key: str, model: str
) -> list[str]:
    """Pull the medical terms worth remembering out of a dictation correction."""
    import json as _json

    from google.genai import types

    client = _client(api_key)
    response = _generate(client,
        model=model,
        contents=[
            "A radiology dictation was transcribed, then corrected by the radiologist.\n\n"
            f"--- TRANSCRIBED ---\n{heard}\n\n--- CORRECTED ---\n{corrected}"
        ],
        config=types.GenerateContentConfig(
            system_instruction=(
                "List the medical terms, drug names, anatomical names and set phrases that "
                "appear in the CORRECTED version and are worth remembering so the transcriber "
                "expects them next time. Terms only - no sentences, no measurements, no "
                "patient details, no common English words. Return JSON only: "
                '{"terms": ["...", "..."]}'
            ),
            response_mime_type="application/json",
            temperature=0.0,
        ),
    )
    data = _json.loads(response.text or "{}")
    return [str(t).strip() for t in (data.get("terms") or []) if str(t).strip()]


def list_models(api_key: str) -> list[str]:
    """Ask Google which models this key can actually call today."""
    from google import genai

    client = genai.Client(api_key=api_key)
    names: list[str] = []
    for model in client.models.list():
        name = (getattr(model, "name", "") or "").replace("models/", "")
        if not name:
            continue
        actions = (
            getattr(model, "supported_actions", None)
            or getattr(model, "supported_generation_methods", None)
            or []
        )
        # Some SDK versions omit the field entirely; keep the model in that case.
        if actions and not any("generatecontent" == str(a).lower().replace("_", "") for a in actions):
            continue
        if "embedding" in name or "aqa" in name or "imagen" in name or "veo" in name:
            continue
        names.append(name)
    names.sort()
    return names


REQUEST_TIMEOUT_MS = 60_000
_RETRY_ATTEMPTS = 3
_TRANSIENT_MARKERS = ("429", "500", "502", "503", "504", "timeout", "deadline",
                      "unavailable", "resource exhausted", "internal error",
                      "connection", "temporarily")


def _client(api_key: str):
    from google import genai

    try:
        from google.genai import types

        return genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
        )
    except Exception:
        # An SDK version without HttpOptions still gets a working client -
        # only the explicit timeout is lost, not the feature.
        return genai.Client(api_key=api_key)


def _generate(client, *, model, contents, config):
    """
    One model call with retries.

    Transient failures (rate limits, 5xx, dropped connections) are retried
    with exponential backoff and jitter; anything else - a bad key, a safety
    block, a malformed request - raises immediately, because retrying it
    would only repeat the same failure slower.
    """
    import random
    import time

    last_exc: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return client.models.generate_content(
                model=model, contents=contents, config=config
            )
        except Exception as exc:
            last_exc = exc
            message = str(exc).lower()
            transient = any(marker in message for marker in _TRANSIENT_MARKERS)
            if not transient or attempt == _RETRY_ATTEMPTS - 1:
                raise
            time.sleep((2 ** attempt) * 0.5 + random.uniform(0, 0.25))
    raise last_exc  # unreachable, keeps type-checkers honest


def _config(temperature: float = 0.0):
    from google.genai import types

    return types.GenerateContentConfig(
        response_mime_type="application/json",
        system_instruction=SYSTEM_PROMPT,
        temperature=temperature,
    )


def structure_with_ai(raw_text: str, api_key: str, model: str) -> list[Block]:
    """Classify raw report text into Blocks using Gemini. Raises on failure."""
    client = _client(api_key)
    response = _generate(client,
        model=model,
        contents=[f"RAW REPORT:\n{raw_text}"],
        config=_config(),
    )
    return _blocks_from_json(response.text)


def extract_text_from_file(
    file_bytes: bytes, mime_type: str, api_key: str, model: str
) -> str:
    """OCR a scanned PDF or a photo of a report into plain text, verbatim."""
    from google.genai import types

    client = _client(api_key)
    response = _generate(client,
        model=model,
        contents=[
            types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
            "Transcribe this radiology report to plain text, verbatim. Preserve every "
            "word, number, unit and measurement and the original line breaks. Do not "
            "summarise, translate, correct or add anything. Return plain text only.",
        ],
        config=types.GenerateContentConfig(temperature=0.0),
    )
    return (response.text or "").strip()


def build_impression_prompt(findings_text: str, template=None) -> str:
    """
    The auto-impression prompt. Pure text, no network - testable offline.

    The findings are the only source of truth. The model may compress and
    reorder, but a finding, number or laterality that is not in the findings
    must not appear in the impression - validate.py will catch it anyway, so
    the prompt says it up front.
    """
    parts: list[str] = [
        "You draft the IMPRESSION section of a radiology report from its FINDINGS.",
        "",
        "Rules:",
        "- Every impression point must come from the findings below. Never add a "
        "finding, a measurement, a number or a laterality that is not there.",
        "- Lead with the clinically most important finding.",
        "- One short point per finding. Omit normal findings unless the study is "
        "entirely normal, in which case the impression is exactly one line saying so.",
        "- Use standard academic radiology phrasing.",
    ]
    if template is not None:
        preferences = [p.strip() for p in (getattr(template, "preferences", None) or []) if p.strip()]
        if preferences:
            parts.append("\nHouse rules from this radiologist:")
            parts.extend(f"- {rule}" for rule in preferences)
        notes = (getattr(template, "style_notes", "") or "").strip()
        if notes:
            parts.append(f"\nHouse-style notes:\n{notes}")
    parts.append(
        "\nReturn JSON only:\n"
        '{"impression": ["first point", "second point"]}'
    )
    parts.append(f"\n--- FINDINGS ---\n{findings_text.strip()}")
    return "\n".join(parts)


def draft_impression(
    findings_text: str, api_key: str, model: str, template=None
) -> list[str]:
    """
    Ask the model for impression bullets from the findings. Raises on failure;
    the caller falls back to impression.propose_from_findings(), which never
    fails and never invents.
    """
    import json as _json

    from google.genai import types

    client = _client(api_key)
    response = _generate(client,
        model=model,
        contents=[build_impression_prompt(findings_text, template)],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.0,
        ),
    )
    data = _json.loads(response.text or "{}")
    out = [str(p).strip().rstrip(".") for p in (data.get("impression") or []) if str(p).strip()]
    if not out:
        raise ValueError("Model returned no impression points.")

    # Negation tripwire: a finding the findings negated must not be asserted
    # in the impression. Omission is fine here - an impression summarises.
    import negation

    negation.assert_polarity(findings_text, "\n".join(out))

    return out


PREFILL_PROMPT = """You are a radiology pre-read assistant. You are shown a medical image
(an X-ray, or a photographed film). Draft preliminary FINDINGS a radiologist will review.

Rules:
- Describe only what is visibly present. If a region is not assessable, say so.
- Use standard academic radiology phrasing, organised by structure.
- Never state a measurement you cannot actually make from the image.
- End with the single line: "Preliminary AI pre-read - requires radiologist review."
- If the image is not a medical image, say exactly that and nothing else.

Return JSON only:
{"findings": "the draft findings text, one finding per line",
 "confidence": "high|medium|low",
 "caveats": ["anything limiting the read - rotation, exposure, cropped anatomy"]}"""


def build_prefill_prompt(context: str = "") -> str:
    """The scan pre-read instruction, with any study context appended."""
    if context.strip():
        return PREFILL_PROMPT + f"\n\nStudy context from the requisition: {context.strip()}"
    return PREFILL_PROMPT


def prefill_from_scan(
    file_bytes: bytes, mime_type: str, api_key: str, model: str, context: str = ""
) -> dict:
    """
    The "bionic pre-read": draft findings from an actual scan image.

    Returns {"findings": str, "confidence": str, "caveats": [str]}. The draft
    lands in the editor as a suggestion; it is never sent anywhere without the
    radiologist rewriting or approving it, and the word-loss audit applies to
    whatever they finally approve, exactly as with typed text.
    """
    import json as _json

    from google.genai import types

    client = _client(api_key)
    response = _generate(client,
        model=model,
        contents=[
            types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
            build_prefill_prompt(context),
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.0,
        ),
    )
    data = _json.loads(response.text or "{}")
    findings = str(data.get("findings") or "").strip()
    if not findings:
        raise ValueError("Model returned no findings.")
    return {
        "findings": findings,
        "confidence": str(data.get("confidence") or "low").strip().lower(),
        "caveats": [str(c).strip() for c in (data.get("caveats") or []) if str(c).strip()],
    }


def draft_impression_from_findings(
    findings_text: str, template=None, api_key: str = "", model: str = ""
) -> list[str]:
    """
    Impression bullets, fast path first.

    The deterministic engine (impression.py) answers in microseconds and
    never invents a word - that is the sub-second guarantee. When a key and
    model are given, the AI draft is attempted on top and used only if it
    passes the negation tripwire; any AI failure falls back to the
    deterministic result instead of surfacing an error.
    """
    import impression

    deterministic = impression.propose_from_findings(findings_text)
    if not deterministic:
        normal = impression.normal_study_line(findings_text)
        deterministic = [normal] if normal else []

    if api_key and model:
        try:
            return draft_impression(findings_text, api_key, model, template)
        except Exception:
            pass  # includes NegationMismatchException - deterministic wins
    return deterministic


SECOND_OPINION_PROMPT = """You are a radiology report safety checker. You are given a complete
report. Deterministic rule checks have already run; you are the second
opinion for what rules cannot see - clinical sense, internal consistency,
findings that contradict the impression in meaning rather than in words.

Judge ONLY what is in the text. Do not invent findings, do not suggest
alternative diagnoses, do not rewrite anything.

Return JSON only, exactly this shape:
{"safe_to_send": true,
 "issues": [{"severity": "critical|warning|note",
             "title": "short statement of the problem",
             "detail": "one or two sentences, quoting the text"}]}

An empty issues list with safe_to_send true is a normal answer."""


def second_opinion(report_text: str, api_key: str, model: str) -> dict:
    """
    The escalation tier of the hybrid validation pipeline: deterministic
    checks first (validate.py, ~ms), this model pass only when the user asks.
    Returns {"safe_to_send": bool, "issues": [{severity,title,detail}]}.
    """
    import json as _json

    from google.genai import types

    client = _client(api_key)
    response = _generate(client,
        model=model,
        contents=[f"REPORT:\n{report_text.strip()}"],
        config=types.GenerateContentConfig(
            system_instruction=SECOND_OPINION_PROMPT,
            response_mime_type="application/json",
            temperature=0.0,
        ),
    )
    data = _json.loads(response.text or "{}")
    issues = []
    for item in data.get("issues") or []:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "note").lower()
        if severity not in ("critical", "warning", "note"):
            severity = "note"
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        issues.append({"severity": severity, "title": title,
                       "detail": str(item.get("detail") or "").strip()})
    return {"safe_to_send": bool(data.get("safe_to_send", not issues)),
            "issues": issues}


def _blocks_from_json(payload: str) -> list[Block]:
    data: Any = json.loads(payload)
    if isinstance(data, dict):
        items = data.get("blocks", [])
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError("Model returned an unexpected JSON shape.")

    blocks: list[Block] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "text")).strip().lower()
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        if kind not in VALID_KINDS:
            kind = "text"
        if kind in ("title", "heading"):
            text = text.upper()
        blocks.append(Block(kind=kind, text=text, raw=text))

    if not blocks:
        raise ValueError("Model returned no usable blocks.")
    return blocks
