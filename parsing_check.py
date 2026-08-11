"""
Offline checks for the advanced parsing layer:

    anatomy.py       - hierarchical findings tree + deterministic coreference
    records.py       - anaphora carry-forward in measurement extraction
    validate.py      - dictation self-correction flag (the human-review limit)
    dictation_fix.py - built-in radiology lexicon suggestions (suggest-only)
    readers.py       - PDF table reconstruction formatter

    python parsing_check.py
"""

from __future__ import annotations

import sys

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("  ok    " if condition else "  FAIL  ") + message)
    if not condition:
        failures.append(message)


# --------------------------------------------------------------------------- #
print("\nanatomy - the findings tree")
# --------------------------------------------------------------------------- #

import anatomy  # noqa: E402

tree = anatomy.findings_tree(
    "Right lobe of liver shows a focal hypoechoic lesion measuring 12 mm.\n"
    "Left lobe of liver is normal in size and echotexture.\n"
    "A well-defined cyst is seen in the left kidney.\n"
    "It measures 2.5 cm and shows no vascularity.\n"
    "Both lung fields are clear."
)
liver = tree.get("ABDOMEN", {}).get("LIVER", {})
check("RIGHT LOBE" in liver and "12 mm" in liver["RIGHT LOBE"][0],
      "the right-lobe finding lands under LIVER > RIGHT LOBE")
check("LEFT LOBE" in liver, "the left lobe gets its own branch")

kidney = tree.get("ABDOMEN", {}).get("LEFT KIDNEY", {})
check(kidney and any("cyst" in s for s in kidney.get("GENERAL", [])),
      "the left kidney cyst lands under LEFT KIDNEY")
check(any("2.5 cm" in s for s in kidney.get("GENERAL", [])),
      "“It measures 2.5 cm” attaches to the kidney - deterministic coreference")

check("CHEST" in tree and any("LUNG" in k for k in tree["CHEST"]),
      "the lungs land under CHEST")

orphan = anatomy.findings_tree("It measures 5 mm.")
check("UNASSIGNED" in orphan,
      "a continuation with nothing to continue is UNASSIGNED, not guessed")

flat = anatomy.flatten(tree)
check(any(path.startswith("ABDOMEN > LIVER > RIGHT LOBE") for path, _ in flat),
      "flatten() gives readable paths")


# --------------------------------------------------------------------------- #
print("\nrecords - anaphora carry-forward")
# --------------------------------------------------------------------------- #

import records  # noqa: E402

measurements = records.extract_measurements(
    "A well-defined cyst is seen in the left kidney. "
    "It measures 2.5 cm and shows no vascularity. "
    "Liver measures 15.1 cm."
)
by_key = {m["key"]: m for m in measurements}
check("cyst" in by_key and by_key["cyst"]["size_mm"] == 25.0,
      f"the pronoun sentence's 2.5 cm reaches the cyst ({by_key.get('cyst')})")
check(by_key["cyst"]["via"] == "anaphora", "and is marked as carried forward")
check("liver" in by_key and by_key["liver"]["via"] == "stated",
      "directly stated measurements are marked stated")

lone = records.extract_measurements("It measures 3 cm.")
check(lone == [], "a measurement with no topic at all is dropped, not guessed")

# The pre-existing behaviour is untouched.
plain = records.extract_measurements("Liver measures 16.2 cm. The left kidney measures 9.8 cm.")
keys = {m["key"] for m in plain}
check(keys == {"liver", "left kidney"}, "plain extraction unchanged")


# --------------------------------------------------------------------------- #
print("\nvalidate - the human-review limit")
# --------------------------------------------------------------------------- #

import validate  # noqa: E402
from hc_format import parse_report  # noqa: E402

confused = parse_report(
    "USG KUB REPORT\n\nFINDINGS:\n"
    "Right kidney shows 12 mm mass, wait left kidney normal right side mass 15 mm.\n\n"
    "IMPRESSION:\n- Renal mass."
)
result = validate.validate(confused.blocks)
check(any("self-correction" in f.title.lower() for f in result.findings),
      "a mid-dictation self-correction is flagged")
check(any("self-correction" in f.title.lower() and f.severity == "critical"
          for f in result.findings),
      "and it is critical - the sign-flow demands a justification")

clean = parse_report(
    "USG KUB REPORT\n\nFINDINGS:\nRight kidney shows a 12 mm mass.\n\n"
    "IMPRESSION:\n- Right renal mass 12 mm."
)
result = validate.validate(clean.blocks)
check(not any("self-correction" in f.title.lower() for f in result.findings),
      "a clean report raises no correction flag")


# --------------------------------------------------------------------------- #
print("\ndictation_fix - the built-in lexicon")
# --------------------------------------------------------------------------- #

import dictation_fix  # noqa: E402

cleaned = dictation_fix.clean("Multiple calculi noted, features of cholelithisis.")
check(any(s.suggested == "cholelithiasis" for s in cleaned.suggestions),
      "a misspelt term gets its suggestion with NO doctor vocabulary configured")
check("cholelithisis" in cleaned.text,
      "and the text itself is untouched - suggest-only, always")

cleaned = dictation_fix.clean("hydro nephrosis with new monia suspected")
check("hydronephrosis" in cleaned.text, "letter-identical joins still auto-apply")
check(any(s.suggested == "pneumonia" for s in cleaned.suggestions),
      "letter-different mishearings still only suggest")


# --------------------------------------------------------------------------- #
print("\nreaders - table reconstruction")
# --------------------------------------------------------------------------- #

import readers  # noqa: E402

two_col = [[["EF", "62 %"], ["LVIDd", "4.8 cm"], ["LVIDs", "3.1 cm"]]]
text = readers.tables_to_text(two_col)
check("EF: 62 %" in text and "LVIDd: 4.8 cm" in text,
      "a two-column table becomes key-value lines")

wide = [[["Parameter", "Right", "Left"],
         ["Kidney length", "9.8 cm", "10.1 cm"],
         ["Cortical thickness", "1.4 cm", "1.5 cm"]]]
text = readers.tables_to_text(wide)
check("Parameter: Kidney length" in text and "Right: 9.8 cm" in text
      and "Left: 10.1 cm" in text,
      "a wide table reattaches every value to its column header")

headerless = [[["12", "34"], ["56", "78"]]]
text = readers.tables_to_text(headerless)
check("12" in text and "78" in text and "Parameter" not in text,
      "a numeric first row is not mistaken for a header")

check(readers.tables_to_text([]) == "", "no tables, no text")
check(readers.pdf_tables(b"not a pdf") == [],
      "junk bytes yield no tables and no crash")


# --------------------------------------------------------------------------- #
print("\nschemas - the tree rides along")
# --------------------------------------------------------------------------- #

import schemas  # noqa: E402

structured = schemas.report_from_blocks(parse_report(
    "USG ABDOMEN REPORT\n\nFINDINGS:\n"
    "Right lobe of liver shows a 12 mm hypoechoic lesion.\n"
    "Left kidney is normal.\n\nIMPRESSION:\n- Hepatic lesion."
).blocks)
check(structured.findings_tree.get("ABDOMEN", {}).get("LIVER", {}).get("RIGHT LOBE"),
      "the structured schema carries the findings tree")


# --------------------------------------------------------------------------- #
print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All parsing checks passed.")
