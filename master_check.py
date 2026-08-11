"""
Offline checks for the master upgrade:

    negation.py       - the polarity tripwire
    dictation_fix.py  - medical ITN and Indic anchors
    validate.py       - clinical context, unit equivalence, YAML rules
    schemas.py        - structured output enforcement
    imgprep.py        - OpenCV preprocessing and deskew
    verify.py         - reconciliation and hash-chained attestation
    templates.py      - macro expansion
    ai_parser.py      - retry wrapper behaviour
    api.py            - the FastAPI layer, called directly

    python master_check.py

Everything offline. OpenCV tests are skipped with a note when cv2 is absent.
"""

from __future__ import annotations

import asyncio
import sys
import time
from unittest import mock

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("  ok    " if condition else "  FAIL  ") + message)
    if not condition:
        failures.append(message)


# --------------------------------------------------------------------------- #
print("\nnegation - the polarity tripwire")
# --------------------------------------------------------------------------- #

import negation  # noqa: E402

polarity = negation.polarity_map(
    "No pneumothorax. There is a large right pleural effusion. "
    "No evidence of free fluid."
)
check(polarity.get("pneumothorax") is True, "a plain 'no X' is negated")
check(polarity.get("pleural effusion") is False, "an asserted finding is positive")
check(polarity.get("free fluid") is True, "'no evidence of X' is negated")

polarity = negation.polarity_map("No pleural effusion but a large mass is seen.")
check(polarity.get("mass") is False, "'but' breaks the negation scope")
check(polarity.get("pleural effusion") is True, "the negated half stays negated")

polarity = negation.polarity_map("Pneumothorax is not seen.")
check(polarity.get("pneumothorax") is True, "post-trigger 'is not seen' negates")

polarity = negation.polarity_map("No significant change in the previously noted mass.")
check(polarity.get("mass") is False,
      "'no significant change' is pseudo-negation - the mass is still there")

mismatches, omissions = negation.check_draft(
    "No active pleuropulmonary lesion. Liver is normal.",
    "Active pleuropulmonary lesion in the right upper zone.",
)
check(len(mismatches) == 1 and mismatches[0].entity == "pleuropulmonary lesion",
      "a flipped negative is caught")

try:
    negation.assert_polarity("No pneumothorax.", "Large pneumothorax present.")
    check(False, "the hard stop did not raise")
except negation.NegationMismatchException as exc:
    check("pneumothorax" in str(exc), "NegationMismatchException names the entity")

omissions = negation.assert_polarity(
    "No pneumothorax. A 4 mm right lower lobe nodule.",
    "Small right lower lobe nodule.",
)
check(omissions == ["pneumothorax"],
      "dropping a negative from a summary is an omission, not an exception")

check(negation.assert_polarity("No free fluid.", "No free fluid is seen.") == [],
      "a preserved negative passes clean")


# --------------------------------------------------------------------------- #
print("\ndictation_fix - medical ITN")
# --------------------------------------------------------------------------- #

import dictation_fix  # noqa: E402

cases = {
    "five by six centimeter mass": "5 x 6 cm mass",
    "grade two spondylolisthesis": "Grade II spondylolisthesis",
    "grade 3 reflux": "Grade III reflux",
    "point five mg per dl": "0.5 mg/dL",
    "two to three millimeter nodule": "2 to 3 mm nodule",
    "heart rate seventy two beats per minute": "heart rate 72 bpm",
    "density of forty hounsfield units": "density of 40 HU",
    "hydro nephrosis noted": "hydronephrosis noted",
    "sub centimeter node": "subcentimeter node",
}
for spoken, written in cases.items():
    got = dictation_fix.clean(spoken).text
    check(got == written, f"{spoken!r} -> {got!r} (wanted {written!r})")

ambiguous = dictation_fix.clean("twenty two to three millimeter nodule")
check(ambiguous.text == "twenty two to three millimeter nodule",
      "an ambiguous descending range is NOT converted")
check(any(s.reason == "range" for s in ambiguous.suggestions),
      "the ambiguous range is flagged for the radiologist")

anchored = dictation_fix.clean("there is o capacity in the right lower zone")
check(any(s.suggested == "opacity" for s in anchored.suggestions),
      "an Indic mishearing is suggested, never applied")
check("o capacity" in anchored.text, "the mishearing itself is left untouched")


# --------------------------------------------------------------------------- #
print("\nvalidate - context, units, YAML rules")
# --------------------------------------------------------------------------- #

import validate  # noqa: E402
from hc_format import parse_report  # noqa: E402

history_ok = parse_report(
    "CT ABDOMEN REPORT\n\n"
    "FINDINGS:\nHistory of left nephrectomy. "
    "A 3 cm enhancing mass in the right kidney.\n\n"
    "IMPRESSION:\n- Right renal mass."
)
result = validate.validate(history_ok.blocks)
check(not any("Impression says" in f.title for f in result.findings),
      "historical left + current right does NOT trip laterality")

genuine = parse_report(
    "USG REPORT\n\nFINDINGS:\nA calculus in the right kidney.\n\n"
    "IMPRESSION:\n- Left renal calculus."
)
result = validate.validate(genuine.blocks)
check(any("Impression says left" in f.title for f in result.findings),
      "a genuine laterality mismatch still trips")

units_ok = parse_report(
    "USG REPORT\n\nFINDINGS:\nA 12 mm calculus in the gallbladder.\n\n"
    "IMPRESSION:\n- 1.2 cm gallbladder calculus."
)
result = validate.validate(units_ok.blocks)
check(not any("1.2 cm" in f.title for f in result.findings),
      "12 mm in findings satisfies 1.2 cm in the impression")

units_bad = parse_report(
    "USG REPORT\n\nFINDINGS:\nA 12 mm calculus in the gallbladder.\n\n"
    "IMPRESSION:\n- 20 mm gallbladder calculus."
)
result = validate.validate(units_bad.blocks)
check(any("20 mm" in f.title and f.severity == "critical" for f in result.findings),
      "a genuinely different measurement is still critical")

brackets = parse_report(
    "USG REPORT\n\nFINDINGS:\nLesion measures [INSERT MEASUREMENT].\n\n"
    "IMPRESSION:\n- Lesion, see above."
)
result = validate.validate(brackets.blocks)
check(any("Unfilled template bracket" in f.title for f in result.findings),
      "[INSERT MEASUREMENT] is caught")

mammo = parse_report(
    "MAMMOGRAPHY REPORT\n\nFINDINGS:\nAn irregular mass in the left breast.\n\n"
    "IMPRESSION:\n- Suspicious left breast mass."
)
result = validate.validate(mammo.blocks)
check(any("BI-RADS" in f.title for f in result.findings),
      "the YAML rule demands BI-RADS on a mammography report")

sentence_kinds = {
    "History of left nephrectomy.": "historical",
    "Known case of carcinoma breast.": "historical",
    "Differential includes lymphoma.": "differential",
    "Family history of polycystic kidneys.": "family",
    "A 3 cm mass in the right kidney.": "current",
}
for sentence, expected in sentence_kinds.items():
    got = validate.sentence_context(sentence)
    check(got == expected, f"context({sentence!r}) = {got} (wanted {expected})")


# --------------------------------------------------------------------------- #
print("\nschemas - structured output enforcement")
# --------------------------------------------------------------------------- #

import schemas  # noqa: E402

structured = schemas.report_from_blocks(parse_report(
    "USG ABDOMEN REPORT\n\n"
    "CLINICAL HISTORY: Pain abdomen.\n\n"
    "FINDINGS:\nLiver:\nEnlarged, 16.8 cm.\nKidneys:\nNormal.\n\n"
    "IMPRESSION:\n- Hepatomegaly.\n- Correlate clinically."
).blocks)
check(structured.study == "USG ABDOMEN REPORT", "study title lands in the schema")
check(structured.clinical_history == "Pain abdomen.", "history lands in the schema")
check("Liver" in structured.findings and "16.8" in structured.findings["Liver"],
      "organ-keyed findings")
check(len(structured.impression) == 2 and structured.impression[0].finding_id == 1,
      "numbered impression items")

try:
    schemas.ImpressionItem(finding_id=1, impression_text="TBD")
    check(False, "a placeholder impression was accepted")
except Exception:
    check(True, "a placeholder impression is rejected by the schema")

items = schemas.impression_items_from_points(
    ["Acute subdural hematoma with midline shift", "Fatty liver"]
)
check(items[0].is_critical and not items[1].is_critical,
      "is_critical is set by triage, per item")


# --------------------------------------------------------------------------- #
print("\nimgprep - OpenCV pipeline")
# --------------------------------------------------------------------------- #

import imgprep  # noqa: E402

try:
    import cv2
    import numpy as np

    canvas = np.full((400, 700), 255, dtype=np.uint8)
    for row, line in enumerate(("FINDINGS NORMAL LIVER", "NO FOCAL LESION SEEN",
                                "IMPRESSION NO ABNORMALITY")):
        cv2.putText(canvas, line, (40, 90 + row * 90),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2, 0, 3)
    matrix = cv2.getRotationMatrix2D((350, 200), 6.0, 1.0)
    skewed = cv2.warpAffine(canvas, matrix, (700, 400), borderValue=255)
    noise = np.random.default_rng(7).integers(-18, 18, skewed.shape, dtype=np.int16)
    noisy = np.clip(skewed.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    ok, encoded = cv2.imencode(".png", noisy)

    processed = imgprep.preprocess(encoded.tobytes())
    check(processed.png.startswith(b"\x89PNG"), "preprocess emits a PNG")
    check(abs(processed.deskew_degrees + 6.0) < 2.5,
          f"a 6-degree skew is detected and corrected "
          f"({processed.deskew_degrees:+.1f} deg)")
    check(any("CLAHE" in s for s in processed.steps)
          and any("adaptive" in s for s in processed.steps),
          "CLAHE and adaptive threshold both ran")

    try:
        imgprep.preprocess(b"not an image at all")
        check(False, "junk bytes were preprocessed")
    except ValueError:
        check(True, "junk bytes are refused")
except ImportError:
    print("  skip  cv2/numpy not installed - preprocessing untested here")

local = imgprep.local_ocr(b"\x89PNG\r\n\x1a\n")
check(local is None or local.engine == "tesseract",
      "local_ocr degrades to None without Tesseract rather than crashing")


# --------------------------------------------------------------------------- #
print("\nverify - reconciliation and attestation")
# --------------------------------------------------------------------------- #

import verify  # noqa: E402

source = ("Liver is enlarged and shows a hypoechoic lesion measuring 12 mm "
          "in segment four. No free fluid.")
mangled = "Liver is enlarged and shows a hypoechoic lesion in segment four."
plan = verify.reconciliation_plan(source, mangled)
check(len(plan) >= 1, f"dropped spans are found ({len(plan)})")
dropped_text = " ".join(p.text for p in plan)
check("12" in dropped_text and "mm" in dropped_text,
      f"the dropped measurement is identified ({dropped_text!r})")
check("free fluid" in dropped_text.lower(), "the dropped negative is identified")

restored, applied, skipped = verify.auto_reconcile(mangled, plan)
check("12 mm" in restored, "the measurement is restored at its anchor")
check("free fluid" in restored.lower(), "the trailing negative is restored")
check(not skipped, f"nothing was skipped ({[s.text for s in skipped]})")
check(verify.reconciliation_plan(source, restored) == [] or
      all("no" == p.text.lower() for p in verify.reconciliation_plan(source, restored)),
      "after reconciliation the alignment is (near) clean")

first = verify.attestation("source text", b"docx-bytes", True)
second = verify.attestation("source text", b"docx-bytes", True,
                            previous_chain=first["chain"])
check(first["chain"] != second["chain"], "the chain advances")
tampered = verify.attestation("source text", b"docx-bytes", False,
                              previous_chain=first["chain"])
check(tampered["chain"] != second["chain"],
      "changing the verdict changes every later hash - tamper-evident")
check(first["source_sha256"] == second["source_sha256"],
      "identical input hashes identically")


# --------------------------------------------------------------------------- #
print("\ntemplates - macros")
# --------------------------------------------------------------------------- #

import templates  # noqa: E402

expanded, used = templates.expand_macros("Chest x-ray. .normalchest", None)
check(".normalchest" in used and "Both lung fields are clear" in expanded,
      "a built-in macro expands")
check("FINDINGS:" in expanded and "IMPRESSION:" in expanded,
      "the expansion is a complete structured block")

doctor = templates.copy_of(templates.HC_FORMAT, "Macro Doc")
doctor = templates.remember_macro(doctor, "normalchest", "My own normal chest text.")
expanded, used = templates.expand_macros(".normalchest", doctor)
check(expanded == "My own normal chest text.",
      "a doctor's macro overrides the built-in")

expanded, used = templates.expand_macros("Liver .nosuchmacro is normal", None)
check(expanded == "Liver .nosuchmacro is normal" and not used,
      "an unknown trigger is left exactly as typed")

try:
    templates.remember_macro(doctor, ".x", "too short")
    check(False, "a two-character trigger was accepted")
except ValueError:
    check(True, "a too-short trigger is refused")

roundtrip = templates.from_dict(templates.to_dict(doctor))
check(roundtrip.macros.get(".normalchest") == "My own normal chest text.",
      "macros survive the save/load round trip")


# --------------------------------------------------------------------------- #
print("\nai_parser - retry wrapper and fast impression")
# --------------------------------------------------------------------------- #

import ai_parser  # noqa: E402


class _FlakyClient:
    def __init__(self, failures_before_success: int, message: str):
        self.calls = 0
        self.failures = failures_before_success
        self.message = message

        parent = self

        class _Models:
            def generate_content(self, **kwargs):
                parent.calls += 1
                if parent.calls <= parent.failures:
                    raise RuntimeError(parent.message)
                return "response"

        self.models = _Models()


with mock.patch("time.sleep"):
    flaky = _FlakyClient(2, "429 Resource exhausted")
    out = ai_parser._generate(flaky, model="m", contents=[], config=None)
    check(out == "response" and flaky.calls == 3,
          f"transient 429s are retried to success ({flaky.calls} calls)")

    fatal = _FlakyClient(99, "API key not valid")
    try:
        ai_parser._generate(fatal, model="m", contents=[], config=None)
        check(False, "a fatal error was retried forever")
    except RuntimeError:
        check(fatal.calls == 1, f"a non-transient error fails fast ({fatal.calls} call)")

    exhausted = _FlakyClient(99, "503 service unavailable")
    try:
        ai_parser._generate(exhausted, model="m", contents=[], config=None)
        check(False, "exhausted retries did not raise")
    except RuntimeError:
        check(exhausted.calls == 3, f"retries stop at the cap ({exhausted.calls} calls)")

started = time.perf_counter()
points = ai_parser.draft_impression_from_findings(
    "Liver is enlarged, measures 16.8 cm.\nNo free fluid.\n"
    "A 6.2 mm calculus in the lower ureter with mild hydronephrosis."
)
elapsed = time.perf_counter() - started
check(bool(points), "the fast impression path returns proposals")
check(elapsed < 1.0, f"and does it in under a second ({elapsed * 1000:.0f} ms)")
for point in points:
    check(not any(ch.isdigit() for ch in point)
          or "16.8" in point or "6.2" in point,
          f"no invented numbers in {point!r}")


# --------------------------------------------------------------------------- #
print("\nstorage - dropped-connection recovery (the Neon idle-suspend case)")
# --------------------------------------------------------------------------- #

import os as _os  # noqa: E402
import shutil as _shutil  # noqa: E402
import tempfile as _tempfile  # noqa: E402

import storage as storage_mod  # noqa: E402

_tmp = _tempfile.mkdtemp(prefix="hc-master-conn-")
conn_store = storage_mod.SqlStore(
    "sqlite:///" + _os.path.join(_tmp, "r.db").replace("\\", "/"))
conn_store.save_report("t", {"id": "abc123", "status": "draft",
                             "report_text": "x", "updated": "2026-08-11"})
_good_conn = conn_store._conn


class _DeadConn:
    """What a Neon-suspended session looks like to the next query."""

    def cursor(self):
        raise RuntimeError(
            "consuming input failed: SSL connection has been closed unexpectedly")

    def close(self):
        pass


class _BadSqlConn:
    def cursor(self):
        raise RuntimeError("syntax error at or near SELECT")

    def close(self):
        pass


conn_store.is_postgres = True          # the retry path is postgres-only
conn_store._conn = _DeadConn()
_reconnects = []
conn_store._reconnect = lambda: (_reconnects.append(1),
                                 setattr(conn_store, "_conn", _good_conn))[0] and None

rows = conn_store.list_reports("t")
check(len(rows) == 1 and len(_reconnects) == 1,
      "a dropped SSL connection reconnects once and the query succeeds")

conn_store._conn = _BadSqlConn()
try:
    conn_store.list_reports("t")
    check(False, "a genuine SQL error was swallowed by the retry")
except RuntimeError:
    check(len(_reconnects) == 1,
          "a genuine SQL error raises without touching the connection")

check(storage_mod.SqlStore._is_disconnect(
    RuntimeError("server closed the connection unexpectedly")),
    "other libpq disconnect wordings are recognised too")
check(not storage_mod.SqlStore._is_disconnect(RuntimeError("deadlock detected")),
      "a deadlock is not mistaken for a disconnect")

conn_store._conn = _good_conn
conn_store.is_postgres = False
conn_store.close()
_shutil.rmtree(_tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
print("\napi - the FastAPI layer, endpoints called directly")
# --------------------------------------------------------------------------- #

import api  # noqa: E402

REPORT = ("CT BRAIN REPORT\n\nPATIENT NAME: Test Patient\nAGE/SEX: 60Y/M\n\n"
          "FINDINGS:\nAcute subdural hematoma with midline shift of 8 mm.\n"
          "No fracture.\n\nIMPRESSION:\n- Acute subdural hematoma.")

health = asyncio.run(api.health())
check(health["ok"] is True, "GET /health answers")

parsed = asyncio.run(api.parse(api.ReportIn(text=REPORT)))
check(parsed.study == "CT BRAIN REPORT" and parsed.impression,
      "POST /parse returns the structured schema")

findings = asyncio.run(api.validate(api.ReportIn(text=REPORT)))
check(isinstance(findings, list), "POST /validate returns findings")

triaged = asyncio.run(api.triage(api.ReportIn(text=REPORT)))
check(triaged["level"] == "stat", "POST /triage sees the subdural")

hl7_out = asyncio.run(api.hl7(api.HL7In(text=REPORT, patient="Test Patient",
                                        age_sex="60Y/M", study="CT BRAIN")))
check(hl7_out["hl7"].startswith("MSH|"), "POST /hl7 emits an ORU message")

fhir_out = asyncio.run(api.fhir(api.HL7In(text=REPORT)))
check(fhir_out["resourceType"] == "Bundle", "POST /fhir emits a Bundle")

aligned = asyncio.run(api.audit_alignment(api.TwoTextsIn(
    source="a b c d", output="a b d")))
check(aligned["dropped_spans"] and aligned["dropped_spans"][0]["text"] == "c",
      "POST /audit finds the dropped word")

expanded = asyncio.run(api.expand_macros(api.MacroIn(text=".normalchest")))
check("Both lung fields are clear" in expanded["text"],
      "POST /expand-macros expands built-ins")

try:
    asyncio.run(api.parse(api.ReportIn(text="   \n  ")))
    check(False, "empty text was parsed")
except Exception:
    check(True, "empty text is refused")


# --------------------------------------------------------------------------- #
print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All master checks passed.")
