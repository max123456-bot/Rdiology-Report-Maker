"""
Edge cases: what happens when the input is nothing like a tidy report.

selftest.py checks correct behaviour on good input. security_check.py attacks
the app deliberately. This covers the messy middle - the text people actually
paste. Reports copied out of a PACS carry control characters. A doctor pastes
an empty box. A file arrives in Devanagari. A line is 100,000 characters long.

Every case here either produced a crash or a silent corruption at some point
during development, so each one is a regression test for a bug that was real.

    python edge_check.py

Offline. No API key, no network.
"""

from __future__ import annotations

import io
import os
import sys

from docx import Document

import ai_parser
import dictation_fix
import readers
import speech
import templates
import validate
import verify
from hc_format import Block, ParseOptions, Span, build_docx, parse_report

HERE = os.path.dirname(os.path.abspath(__file__))
problems: list[tuple[str, str, str]] = []


def problem(area: str, what: str, detail: str = "") -> None:
    problems.append((area, what, detail))


def guard(area: str, label: str, fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - the point is to catch everything
        problem(area, f"{label} raises {type(exc).__name__}", str(exc)[:160])
        return None


# Text that has broken this app at some point.
HOSTILE = {
    "empty": "",
    "whitespace only": "   \n\t\n   ",
    "single character": "x",
    "one line, no heading": "MRI BRAIN",
    "heading with no content": "REPORT\nFINDINGS:\nIMPRESSION:",
    "starts with a heading": "FINDINGS:\nNormal.",
    "accented latin": "MRI BRAİN ÖZET\n\nFINDINGS:\nLezyon 2,4 cm × 1,9 cm.\n\nIMPRESSION:\nNormal.",
    "devanagari": "एमआरआई ब्रेन\n\nFINDINGS:\nसामान्य अध्ययन।\n\nIMPRESSION:\nसामान्य।",
    "arabic (right to left)": "REPORT\n\nFINDINGS:\nتقرير طبيعي\n\nIMPRESSION:\nطبيعي.",
    "very long line": "REPORT\nFINDINGS:\n" + ("word " * 20000),
    "very many lines": "REPORT\n" + "\n".join(f"line {i}" for i in range(5000)),
    "colons everywhere": "R\nFINDINGS:\n" + "\n".join("a:b:c:d" for _ in range(200)),
    # These two crashed python-docx outright: a PACS or PDF export routinely
    # carries stray control bytes, and one of them took the whole app down.
    "null bytes": "REPORT\x00\nFINDINGS:\nText\x00here.",
    "control characters": "REPORT\n\x01\x02\x03\nFINDINGS:\nNormal.",
    "DEL and C1 range": "REPORT\n\x7f\x9f\nFINDINGS:\nNormal.",
    "windows line endings": "REPORT\r\n\r\nFINDINGS:\r\nNormal.\r\n",
    "tabs": "REPORT\n\tFINDINGS:\n\t\tNormal.",
    "emoji": "REPORT \U0001fa7a\n\nFINDINGS:\nNormal.\n\nIMPRESSION:\nFine.",
    "html in the text": "REPORT\n\nFINDINGS:\n<script>alert(1)</script>\n\nIMPRESSION:\nX.",
    "xml special characters": 'REPORT\n\nFINDINGS:\n" & < > \' ]]>\n\nIMPRESSION:\nNormal.',
}


def check_parsing() -> None:
    print("Parsing, rendering and auditing hostile input")
    for label, text in HOSTILE.items():
        for as_is in (False, True):
            tag = f"{label} (as-is={as_is})"
            result = guard("parse", tag,
                           lambda t=text, a=as_is: parse_report(t, ParseOptions(preserve_as_is=a)))
            if result is None:
                continue
            docx = guard("render", tag, lambda b=result.blocks: build_docx(b))
            if docx is None:
                continue
            guard("docx integrity", tag, lambda d=docx: Document(io.BytesIO(d)))
            guard("audit", tag,
                  lambda t=text, d=docx, a=as_is: verify.audit(t, d, preserve_as_is=a))
            guard("validate", tag, lambda b=result.blocks: validate.validate(b))
    print(f"  {len(HOSTILE)} inputs x 2 modes rendered, opened and audited")


def check_word_preservation() -> None:
    print("\nWord preservation under every parsing option")
    combinations = [
        ParseOptions(),
        ParseOptions(split_sentences=True),
        ParseOptions(bold_comment_bullets=True),
        ParseOptions(inline_meta_headings=False),
        ParseOptions(max_subheading_len=20),
        ParseOptions(max_subheading_len=80),
        ParseOptions(preserve_as_is=True),
    ]
    count = 0
    for filename in sorted(os.listdir(os.path.join(HERE, "samples"))):
        text = open(os.path.join(HERE, "samples", filename), encoding="utf-8").read()
        for opts in combinations:
            docx = build_docx(parse_report(text, opts).blocks)
            report = verify.audit(text, docx, preserve_as_is=opts.preserve_as_is)
            count += 1
            if not report.ok:
                problem("word loss", f"{filename} with {opts}", report.summary[:150])
    print(f"  {count} sample/option combinations, every word preserved")


def check_blocks() -> None:
    print("\nMalformed blocks must render rather than crash")
    cases = [
        ("spans", Block("bold_bullet", "abc",
                        spans=[Span("a"), Span("b", bold=True), Span("c")])),
        ("empty block", Block("bullet", "")),
        ("inline heading with spans", Block("heading_inline", "NAME:", trailer="X",
                                            trailer_spans=[Span("X", italic=True)])),
        ("5000-character title", Block("title", "T" * 5000)),
        ("empty span", Block("text", "x", spans=[Span("")])),
        ("spans disagree with text", Block("text", "mismatch", spans=[Span("different")])),
        ("indented verbatim", Block("verbatim", "   indented   ")),
        ("unknown kind", Block("not_a_real_kind", "fallback")),
        ("control chars in a span", Block("text", "x", spans=[Span("a\x00b\x01c")])),
    ]
    for label, block in cases:
        docx = guard("render", label, lambda b=block: build_docx([b]))
        if docx:
            guard("docx integrity", label, lambda d=docx: Document(io.BytesIO(d)))
    print(f"  {len(cases)} malformed blocks rendered into valid documents")


def check_spoken_numbers() -> None:
    print("\nSpoken numbers")
    converts = {
        "point five": "0.5",
        "zero point five": "0.5",
        "one hundred": "100",
        "one hundred and five": "105",
        "twenty three point four five": "23.45",
        "fifteen": "15",
        "seventy year old male": "70 year old male",   # number yes, grammar untouched
        "twenty three weeks": "23 weeks",
    }
    for spoken, expected in converts.items():
        out, _ = dictation_fix.spoken_numbers(spoken)
        out, _ = dictation_fix.units(out)
        if out != expected:
            problem("spoken numbers", f"{spoken!r} -> {out!r}", f"expected {expected!r}")

    # Newly supported, and the ambiguity that must NOT be guessed.
    converts.update({
        "two thousand HU": "2000 HU",
        "two lakh cells": "200000 cells",
        "nine point eight into four point four cm": "9.8 x 4.4 cm",
    })
    for spoken, expected in converts.items():
        out, _ = dictation_fix.spoken_numbers(spoken)
        out, _ = dictation_fix.units(out)
        if out != expected:
            problem("spoken numbers", f"{spoken!r} -> {out!r}", f"expected {expected!r}")

    # "two fifty" is 250 to an Indian English speaker and 52 to an additive
    # parser. A wrong measurement is worse than an unconverted one, so these
    # must come through as words.
    for ambiguous in ["volume is two fifty ml", "three twenty HU", "two three mm"]:
        out, _ = dictation_fix.spoken_numbers(ambiguous)
        out, _ = dictation_fix.units(out)
        if any(ch.isdigit() for ch in out):
            problem("spoken numbers", f"guessed at ambiguous {ambiguous!r}",
                    f"-> {out!r} - refusing is the safe behaviour")

    # A lone number word, or one used as a figure of speech, is prose.
    unchanged = [
        "point", "one", "and", "hundred", "point point", "one point",
        "No focal lesion is seen.",
        "Grade I changes.",
        "one previous study for comparison",
        "Point tenderness in the right upper quadrant.",
        "The mass is in the left lobe.",
        "and there is no free fluid",
        "a hundred percent normal study",
        "one and a half centimetres",
        "Second and third ribs.",
    ]
    for prose in unchanged:
        out, _ = dictation_fix.spoken_numbers(prose)
        out, _ = dictation_fix.units(out)
        if out != prose:
            problem("spoken numbers", f"altered prose {prose!r}", f"-> {out!r}")

    print(f"  {len(converts)} conversions correct, {len(unchanged)} pieces of prose untouched")


def check_dictation_robustness() -> None:
    print("\nDictation cleanup with junk input")
    guard("dictation", "clean('')", lambda: dictation_fix.clean("", []))
    guard("dictation", "clean with junk vocabulary",
          lambda: dictation_fix.clean("text", ["", "  ", None, 123, "cholelithiasis"]))
    guard("dictation", "near_misses with empty vocabulary",
          lambda: dictation_fix.near_misses("abc", []))
    hits = dictation_fix.near_misses("abc", ["", "  ", None, 123])
    if hits:
        problem("dictation", "junk vocabulary produced suggestions", str(hits))
    # A correct term must never be suggested against itself.
    if dictation_fix.near_misses("cholelithiasis present", ["cholelithiasis"]):
        problem("dictation", "a correct term was flagged as a near miss")
    print("  junk vocabulary and empty input handled")


def check_templates() -> None:
    print("\nHand-edited template files")
    junk = [
        {}, {"name": ""}, {"name": None}, {"name": 123},
        {"name": "X", "font_size": -5}, {"name": "X", "font_size": 1e9},
        {"name": "X", "line_spacing": -1}, {"name": "X", "margin_top": -3},
        {"name": "X", "font_color": "#GGGGGG"}, {"name": "X", "styles": None},
        {"name": "X", "styles": {"title": None}}, {"name": "X", "styles": {"nope": {}}},
        {"name": "X", "corrections": [None, 5, {}]},
        {"name": "X", "answered": "not a dict"},
        {"name": "X", "vocabulary": [None, 1, "ok"]},
    ]
    for i, payload in enumerate(junk):
        tpl = guard("templates", f"from_dict case {i}", lambda p=payload: templates.from_dict(p))
        if tpl is not None:
            guard("templates", f"render with case {i}",
                  lambda t=tpl: build_docx([Block("title", "X"), Block("bullet", "y")], template=t))
    print(f"  {len(junk)} malformed template payloads coerced and rendered")


def check_readers_and_speech() -> None:
    print("\nUploads and model IDs")
    files = [("a.pdf", b""), ("a.docx", b""), ("a.txt", b""),
             ("a.pdf", b"%PDF-1.4 truncated"), ("a.docx", b"PK\x03\x04"),
             ("noext", b"hello"), ("a.PDF", b"junk"), ("a.png", b"x")]
    for name, data in files:
        try:
            readers.read_any(name, data)
        except (readers.NeedsOCR, readers.UnreadableFile):
            pass
        except Exception as exc:  # noqa: BLE001
            problem("readers", f"{name} raises {type(exc).__name__}", str(exc)[:120])

    for model in ["", "  ", "a/b", "a//b", "../x", "a b/c", "A/B-1.2_3"]:
        try:
            speech.validate_model_id(model)
        except speech.SpeechError:
            pass
        except Exception as exc:  # noqa: BLE001
            problem("speech", f"validate_model_id({model!r}) raises {type(exc).__name__}",
                    str(exc)[:120])
    guard("speech", "to_wav on junk bytes", lambda: speech.to_wav(b"not audio", "audio/wav"))
    print(f"  {len(files)} corrupt uploads and 7 model IDs handled")


def check_prompts() -> None:
    print("\nPrompt builders with degenerate templates")
    for tpl in [templates.HC_FORMAT,
                templates.copy_of(templates.HC_FORMAT, "X"),
                templates.from_dict({"name": "Y"})]:
        guard("ai_parser", "build_draft_prompt",
              lambda t=tpl: ai_parser.build_draft_prompt(t, ""))
        guard("ai_parser", "build_transcribe_prompt",
              lambda t=tpl: ai_parser.build_transcribe_prompt(t, ""))
    guard("ai_parser", "missing_facts on empty input", lambda: ai_parser.missing_facts("", ""))

    # Unusable model output MUST raise, because that is what makes the app fall
    # back to the rule-based parser instead of shipping a broken report.
    try:
        ai_parser._blocks_from_json('{"blocks":[{"kind":"nope"}]}')
        problem("ai_parser", "unusable model output did not raise",
                "the rule-based fallback would never trigger")
    except ValueError:
        pass
    print("  prompts build, and unusable AI output still raises for the fallback")


def main() -> int:
    print("Edge cases — the messy input people actually paste\n")
    check_parsing()
    check_word_preservation()
    check_blocks()
    check_spoken_numbers()
    check_dictation_robustness()
    check_templates()
    check_readers_and_speech()
    check_prompts()

    print("\n" + "=" * 70)
    if not problems:
        print("Nothing broke. Every edge case rendered a valid document.")
        return 0
    print(f"{len(problems)} problem(s):\n")
    for area, what, detail in problems:
        print(f"  [{area}] {what}")
        if detail:
            print(f"      {detail}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
