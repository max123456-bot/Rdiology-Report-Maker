"""
Live end-to-end check of the AI features. Needs a Gemini API key.

Everything else in this project is tested offline by selftest.py. This script
covers the parts that genuinely need the network: the model list, drafting with
clarifying questions, distilling rules from a correction, and proving a learned
rule actually changes the next draft.

The key is read from the environment and never written anywhere. It is not
printed, not saved to a template, and not committed to any file.

    PowerShell:   $env:GEMINI_API_KEY = "your-key"; python live_check.py
    Git Bash:     GEMINI_API_KEY=your-key python live_check.py

Optional:  $env:GEMINI_MODEL = "gemini-2.5-pro"
"""

from __future__ import annotations

import os
import sys

import ai_parser
import templates

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
TEMP_NAME = "__live_check__"

# Deliberately ambiguous: no side given for the kidney, and "no chole" is
# shorthand that could be read more than one way.
NOTES = """mult gallstones 4-11mm, no chole
liver 14.2cm normal, no SOL
kidney 9.8 x 4.4cm, no calculus"""

failures: list[str] = []


def check(ok: bool, message: str) -> None:
    print(("  PASS  " if ok else "  FAIL  ") + message)
    if not ok:
        failures.append(message)


def main() -> int:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        print(
            "GEMINI_API_KEY is not set.\n\n"
            '  PowerShell:  $env:GEMINI_API_KEY = "your-key"; python live_check.py\n'
            "  Git Bash:    GEMINI_API_KEY=your-key python live_check.py"
        )
        return 2

    print(f"Model: {MODEL}\n")

    # ---------------------------------------------------------------- #
    print("1. The key can reach the API and list models")
    try:
        models = ai_parser.list_models(key)
    except Exception as exc:
        print(f"  FAIL  could not list models: {exc}")
        return 1
    check(bool(models), f"{len(models)} model(s) available")
    check(MODEL in models, f"{MODEL} is callable with this key")
    if MODEL not in models:
        print(f"        available: {', '.join(models[:8])}")
        return 1

    # ---------------------------------------------------------------- #
    print("\n2. Drafting, and asking about what is ambiguous")
    doctor = templates.copy_of(templates.HC_FORMAT, TEMP_NAME, doctor="Dr. Live Check")
    try:
        first = ai_parser.draft_with_questions(
            NOTES, doctor, key, MODEL, section="the IMPRESSION only"
        )
    except Exception as exc:
        print(f"  FAIL  drafting failed: {exc}")
        return 1

    check(bool(first["draft"]), "a draft came back")
    print("\n  --- draft ---")
    for line in first["draft"].splitlines():
        print("  " + line)

    if first["questions"]:
        print(f"\n  {len(first['questions'])} question(s) raised:")
        for q in first["questions"]:
            print(f"    Q: {q['question']}")
            if q.get("why"):
                print(f"       why: {q['why']}")
            print(f"       options: {', '.join(q['options']) or '(none)'}")
        check(
            any(o for q in first["questions"] for o in q["options"]),
            "questions came with concrete options to choose from",
        )
    else:
        print("\n  No questions raised. Not a failure - it only asks when the answer")
        print("  would change the report, and it may have read the notes confidently.")

    for assumption in first["assumptions"]:
        print(f"  Assumed: {assumption}")

    # ---------------------------------------------------------------- #
    print("\n3. The measurement guard")
    dropped = ai_parser.missing_facts(NOTES, first["draft"])
    check(not dropped, "every number from the notes survived into the draft"
          if not dropped else f"numbers missing from the draft: {', '.join(dropped)}")

    # ---------------------------------------------------------------- #
    print("\n4. Learning from a correction")
    before = "IMPRESSION:\n- Multiple stones in the gallbladder.\n- Normal liver."
    after = (
        "IMPRESSION:\n1. Multiple calculi in the gallbladder.\n2. Normal hepatic study.\n\n"
        "Please correlate clinically."
    )
    try:
        rules = ai_parser.distill_preferences(
            before, after, doctor, key, MODEL,
            note="I write calculi, I number the impression, and I always close with the "
                 "correlate line.",
        )
    except Exception as exc:
        print(f"  FAIL  distilling failed: {exc}")
        return 1

    check(bool(rules), f"{len(rules)} reusable rule(s) distilled")
    for rule in rules:
        print(f"    - {rule}")
    check(
        any("calculi" in r.lower() for r in rules),
        "it learned the calculi/stones preference",
    )
    check(
        not any(r.strip().startswith("The liver") for r in rules),
        "rules are general, not facts about this one report",
    )

    # ---------------------------------------------------------------- #
    print("\n5. The learned rule changes the next draft")
    taught = templates.remember_correction(doctor, before, after, rules=rules)
    templates.save(taught)
    reloaded = templates.load_all()[TEMP_NAME]
    check(
        len(reloaded.preferences) == len(rules),
        "the rules survived a save and reload from disk",
    )

    try:
        second = ai_parser.draft_with_questions(
            NOTES, reloaded, key, MODEL, section="the IMPRESSION only"
        )
    except Exception as exc:
        print(f"  FAIL  redraft failed: {exc}")
        templates.delete(TEMP_NAME)
        return 1

    print("\n  --- draft after learning ---")
    for line in second["draft"].splitlines():
        print("  " + line)

    body = second["draft"].lower()
    check("calculi" in body, "the learned wording 'calculi' was applied")
    check("stones" not in body, "the corrected wording 'stones' was dropped")

    # ---------------------------------------------------------------- #
    print("\n6. Dictation priming (offline part — the audio path needs a real recording)")
    voice = templates.remember_dictation_fix(reloaded, "colic list", "cholelithiasis")
    voice = templates.remember_vocabulary(voice, ["craniocaudal", "hydronephrosis"])
    prompt = ai_parser.build_transcribe_prompt(voice, context="USG abdomen")
    check("cholelithiasis" in prompt, "learned vocabulary reaches the transcriber")
    check('heard "colic list"' in prompt, "past mishearings reach the transcriber")
    check("USG abdomen" in prompt, "the study context reaches the transcriber")
    print("        To test real audio: open the 🎙 Dictate tab, record a sentence with a")
    print("        measurement in it, and check the number came through exactly.")

    templates.delete(TEMP_NAME)
    print(f"\n(temporary template {TEMP_NAME} removed)")

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for message in failures:
            print("  -", message)
        return 1
    print("Live check passed: it asks, it learns, and the lesson sticks.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        templates.delete(TEMP_NAME)
        sys.exit(130)
