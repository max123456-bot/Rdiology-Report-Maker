"""
Offline checks for the bulk-processing engine (batch.py):

    split_pasted    the --- separator
    from_csv        spreadsheet rows become reports with real headings
    route_template  each report finds its own doctor's template
    process_one     parse -> build -> audit -> QC, exceptions contained
    manifest_csv    the medico-legal hash manifest
    zip_bytes       docx files + manifest, names de-duplicated

    python batch_check.py
"""

from __future__ import annotations

import csv
import hashlib
import io
import sys
import zipfile

import batch
import templates
from hc_format import ParseOptions

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("  ok    " if condition else "  FAIL  ") + message)
    if not condition:
        failures.append(message)


OPTS = ParseOptions()
REPORT_A = ("USG ABDOMEN REPORT\n\nPATIENT NAME: Anita Sharma\nAGE/SEX: 40Y/F\n\n"
            "FINDINGS:\nLiver measures 15.1 cm, normal echotexture.\n\n"
            "IMPRESSION:\n- Normal study.\n\nDr. Rakesh Sharma")
REPORT_B = ("CT BRAIN REPORT\n\nFINDINGS:\nAcute subdural hematoma with midline "
            "shift.\n\nIMPRESSION:\n- Acute subdural hematoma.\n\nDr. Priya Verma")


# --------------------------------------------------------------------------- #
print("\nintake")
# --------------------------------------------------------------------------- #

chunks = batch.split_pasted(f"{REPORT_A}\n---\n{REPORT_B}\n----\nthird")
check(len(chunks) == 3, f"--- splits the paste ({len(chunks)})")

CSV_SHEET = (
    "Patient_Name,Age_Sex,Study_Name,Patient_ID,Raw_Dictation_Text\n"
    "Anita Sharma,40Y/F,USG Abdomen,MRN9,\"FINDINGS:\nLiver is normal.\n"
    "IMPRESSION:\nNormal study.\"\n"
    ",,,,\n"
    "Rahul Verma,55Y/M,CT Brain,MRN10,\"FINDINGS:\nNo acute infarct.\n"
    "IMPRESSION:\nNormal CT.\"\n"
).encode("utf-8")
items = batch.from_csv(CSV_SHEET)
check(len(items) == 2, f"two data rows become two reports ({len(items)})")
check(items[0].patient == "Anita Sharma" and items[0].age_sex == "40Y/F",
      "metadata columns are read")
check("PATIENT NAME: Anita Sharma" in items[0].text
      and "AGE/SEX: 40Y/F" in items[0].text,
      "the metadata becomes the report's own headings")
check(items[0].text.startswith("USG ABDOMEN REPORT"),
      "the study name becomes the title line")
check(items[0].label == "Anita_Sharma", "the row is labelled by patient")

try:
    batch.from_csv(b"colA,colB\n1,2\n")
    check(False, "a sheet with no dictation column was accepted")
except ValueError as exc:
    check("dictation column" in str(exc).lower(), "a missing dictation column says so")

aliased = batch.from_csv(b"name,report\nX Y,\"FINDINGS: ok\"\n")
check(len(aliased) == 1, "column aliases (name/report) are understood")


# --------------------------------------------------------------------------- #
print("\nrouting")
# --------------------------------------------------------------------------- #

default = templates.HC_FORMAT
dr_sharma = templates.copy_of(default, "Sharma style", doctor="Dr. Rakesh Sharma")
dr_verma = templates.copy_of(default, "Verma style", doctor="Dr. Priya Verma")
pool = {"HC FORMAT (default)": default, "Sharma style": dr_sharma,
        "Verma style": dr_verma}

routed, how = batch.route_template(REPORT_A, pool, default)
check(routed is dr_sharma and "signature" in how,
      f"report A routes to Dr. Sharma by signature ({how})")
routed, how = batch.route_template(REPORT_B, pool, default)
check(routed is dr_verma, "report B routes to Dr. Verma")
routed, how = batch.route_template("FINDINGS:\nNormal.\n", pool, default)
check(routed is default and "no doctor name" in how,
      "no name falls back to the selected template, and says so")
both = REPORT_A + "\nDr. Priya Verma"
routed, how = batch.route_template(both, pool, default)
check(routed is default and "ambiguous" in how,
      "two signatures is ambiguous - fallback, never a guess")


# --------------------------------------------------------------------------- #
print("\nprocessing")
# --------------------------------------------------------------------------- #

result = batch.process_one("a", REPORT_A, dr_sharma, letterhead=None,
                           page_numbers=False, letterhead_text="", opts=OPTS)
check(result.docx.startswith(b"PK"), "a real .docx comes out")
check(result.audit_ok, f"the word-loss audit passes ({result.audit_summary})")
check(result.patient == "Anita Sharma" and result.modality == "USG",
      "patient and modality reach the QC row")
check(result.urgency == "routine" and not result.qc_critical, "a clean report is Ready")
check(result.ready, "ready means ready")
check(result.source_sha256 == hashlib.sha256(REPORT_A.encode()).hexdigest(),
      "the source hash is the hash of the exact source")

stat_result = batch.process_one("b", REPORT_B, default, letterhead=None,
                                page_numbers=False, letterhead_text="", opts=OPTS)
check(stat_result.urgency == "stat", "the subdural batch row is marked stat")

broken = batch.process_one("c", "   ", default, letterhead=None,
                           page_numbers=False, letterhead_text="", opts=OPTS)
check(bool(broken.error) and not broken.ready,
      "an empty row errors on its own without killing the batch")


# --------------------------------------------------------------------------- #
print("\nmanifest and zip")
# --------------------------------------------------------------------------- #

blob = batch.zip_bytes([result, stat_result, broken])
with zipfile.ZipFile(io.BytesIO(blob)) as zf:
    names = zf.namelist()
    check("batch_audit_manifest.csv" in names, "the manifest travels inside the ZIP")
    check(sum(1 for n in names if n.endswith(".docx")) == 2,
          "two good reports, two .docx files - the errored row ships no file")
    manifest = zf.read("batch_audit_manifest.csv").decode("utf-8")

rows = list(csv.DictReader(io.StringIO(manifest)))
check(len(rows) == 3, "every report, including the errored one, is in the manifest")
by_label = {r["source_label"]: r for r in rows}
check(by_label["a"]["word_loss_audit"] == "PASS", "the audit verdict is recorded")
check(by_label["a"]["sha256_docx"]
      == hashlib.sha256(result.docx).hexdigest(),
      "the manifest's docx hash matches the shipped bytes exactly")
check(by_label["b"]["urgency"] == "stat", "urgency is recorded")
check(by_label["c"]["word_loss_audit"] == "ERROR" and by_label["c"]["error"],
      "the errored row says ERROR and why")
check(all(r["generated_utc"] for r in rows), "every row is timestamped")

dup = batch.process_one("a2", REPORT_A, dr_sharma, letterhead=None,
                        page_numbers=False, letterhead_text="", opts=OPTS)
blob = batch.zip_bytes([result, dup])
with zipfile.ZipFile(io.BytesIO(blob)) as zf:
    docx_names = [n for n in zf.namelist() if n.endswith(".docx")]
    check(len(docx_names) == 2 and len(set(docx_names)) == 2,
          "two reports with the same title get distinct filenames")


# --------------------------------------------------------------------------- #
print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All batch checks passed.")
