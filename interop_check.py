"""
Offline checks for the hospital-facing layer:

    interop.py     - HL7 ORU^R01 field positions, escaping; FHIR bundle shape
    dicom_meta.py  - header parsing (when pydicom is installed) and cross-checks
    notify.py      - alert building; unconfigured channels fail loudly, not silently
    providers.py   - registry selection and fallback
    workers.py     - background jobs finish, fail and report honestly

    python interop_check.py

No network. pydicom parsing is skipped with a note if the package is absent.
"""

from __future__ import annotations

import base64
import json
import sys

import dicom_meta
import interop
import notify
import providers
import workers

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("  ok    " if condition else "  FAIL  ") + message)
    if not condition:
        failures.append(message)


RECORD = {
    "id": "abc123def456",
    "created": "2026-08-11T05:00:00+00:00",
    "updated": "2026-08-11T05:30:00+00:00",
    "status": "signed",
    "patient": "Mrs. Sunita Devi",
    "age_sex": "45 Years / Female",
    "referrer": "Dr. Mehta",
    "study": "USG ABDOMEN REPORT",
    "modality": "USG",
    "patient_key": "sunita-devi-f",
    "urgency": "stat",
    "triage_terms": ["free fluid", "ruptured aneurysm"],
    "report_text": ("USG ABDOMEN REPORT\n\nFINDINGS:\nGross free fluid. "
                    "Leaking aortic aneurysm | 6.4 cm.\n\n"
                    "IMPRESSION:\nRuptured abdominal aortic aneurysm."),
    "signed_by": "dr-a",
    "signed_at": "2026-08-11T05:30:00+00:00",
}


# --------------------------------------------------------------------------- #
print("\ninterop - HL7 ORU^R01")
# --------------------------------------------------------------------------- #

message = interop.hl7_oru(RECORD, facility="HC CLINIC")
segments = message.strip("\r").split("\r")
check(segments[0].startswith("MSH|^~\\&|HCFORMAT|"), "MSH leads with the sending app")
msh = segments[0].split("|")
check(msh[8] == "ORU^R01^ORU_R01", f"message type is ORU^R01 ({msh[8]})")
check(msh[9] == RECORD["id"], "the record id is the message control id")
check(msh[11] == "2.5", "HL7 version 2.5")

pid = next(s for s in segments if s.startswith("PID")).split("|")
check(pid[5] == "DEVI^SUNITA", f"PID-5 is family^given ({pid[5]})")
check(pid[8] == "F", f"PID-8 is the sex ({pid[8]})")

obr = next(s for s in segments if s.startswith("OBR")).split("|")
check("USG ABDOMEN REPORT" in obr[4], f"OBR-4 carries the study ({obr[4]})")
check(obr[5] == "S", "OBR-5 marks a stat report S")
check(obr[25] == "F", "OBR-25 is F for a signed report")

obx = [s for s in segments if s.startswith("OBX")]
report_lines = [ln for ln in RECORD["report_text"].splitlines() if ln.strip()]
check(len(obx) >= len(report_lines), f"one OBX per report line at least ({len(obx)})")
check(any("\\F\\" in s for s in obx), "a literal | in the text is escaped as \\F\\")
check(not any("\n" in s for s in segments), "no raw newlines inside a segment")
check(any("19005-8" in s for s in obx), "the impression is coded separately (LOINC 19005-8)")

draft_message = interop.hl7_oru({**RECORD, "status": "draft", "urgency": "routine"})
draft_obr = next(s for s in draft_message.split("\r") if s.startswith("OBR")).split("|")
check(draft_obr[25] == "P", "a draft exports as preliminary (OBR-25 P)")
check(draft_obr[5] == "R", "a routine report is OBR-5 R")

check(interop.hl7_name("Mrs. Sunita Devi") == "DEVI^SUNITA", "name inverts to family^given")
check(interop.hl7_name("Ramu") == "RAMU", "a single name stays whole")
check(interop.sex_of("45Y/M") == "M" and interop.sex_of("45 / Female") == "F"
      and interop.sex_of("") == "", "sex parsing")

imp = interop.impression_of(RECORD["report_text"])
check(imp == "Ruptured abdominal aortic aneurysm.", f"impression extraction ({imp!r})")


# --------------------------------------------------------------------------- #
print("\ninterop - FHIR")
# --------------------------------------------------------------------------- #

bundle = interop.fhir_diagnostic_report(RECORD, docx_bytes=b"PK-fake-docx")
check(bundle["resourceType"] == "Bundle" and bundle["type"] == "collection",
      "a collection bundle comes back")
resources = {e["resource"]["resourceType"]: e["resource"] for e in bundle["entry"]}
check(set(resources) == {"Patient", "DiagnosticReport"}, "Patient + DiagnosticReport")
patient = resources["Patient"]
check(patient["gender"] == "female", "gender mapped from AGE/SEX")
report = resources["DiagnosticReport"]
check(report["status"] == "final", "a signed report is final")
check(report["code"]["coding"][0]["code"] == "18748-4", "LOINC-coded as an imaging study")
check(report["conclusion"] == "Ruptured abdominal aortic aneurysm.",
      "the conclusion is the impression")
forms = report["presentedForm"]
check(len(forms) == 2, "text and .docx are both attached")
decoded = base64.b64decode(forms[0]["data"]).decode("utf-8")
check(decoded == RECORD["report_text"], "the attached text round-trips byte for byte")
check(json.loads(interop.fhir_json(RECORD)), "fhir_json emits valid JSON")
draft_bundle = interop.fhir_diagnostic_report({**RECORD, "status": "draft"})
draft_report = draft_bundle["entry"][1]["resource"]
check(draft_report["status"] == "preliminary", "a draft is preliminary in FHIR")


# --------------------------------------------------------------------------- #
print("\ndicom_meta")
# --------------------------------------------------------------------------- #

meta = dicom_meta.StudyMeta(
    patient_name="DEVI SUNITA", sex="F", age_years=45, modality="US",
    study_description="USG ABDOMEN", laterality="L",
)

issues = dicom_meta.cross_check(
    meta, "Right kidney shows hydronephrosis.", "Sunita Devi", "45Y/F"
)
titles = [f.title for f in issues]
check(any("left side" in t or "the left" in t for t in titles),
      f"scan-left vs report-right is caught ({titles})")

issues = dicom_meta.cross_check(
    dicom_meta.StudyMeta(sex="M"), "The uterus is anteverted, normal size.", "", ""
)
check(any(f.severity == "critical" for f in issues), "male patient + uterus is critical")

issues = dicom_meta.cross_check(
    dicom_meta.StudyMeta(sex="F"), "The prostate measures 30 cc.", "", ""
)
check(any(f.severity == "critical" for f in issues), "female patient + prostate is critical")

issues = dicom_meta.cross_check(
    dicom_meta.StudyMeta(patient_name="RAMESH KUMAR"),
    "Liver is normal.", "Sunita Devi", ""
)
check(any("different patients" in f.title for f in issues), "a name mismatch is caught")

issues = dicom_meta.cross_check(meta, "Left kidney is obstructed.", "Sunita Devi", "45Y/F")
check(not issues, f"an agreeing report raises nothing ({[f.title for f in issues]})")

issues = dicom_meta.cross_check(
    meta, "CT KUB REPORT\n\nLeft kidney is obstructed.", "Sunita Devi", "45Y/F"
)
check(any("reads like" in f.title for f in issues),
      "a title claiming the wrong modality is caught")

try:
    import pydicom
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    import io as _io

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = generate_uid()
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = file_meta
    ds.PatientName = "Devi^Sunita"
    ds.PatientSex = "F"
    ds.PatientAge = "045Y"
    ds.Modality = "US"
    ds.StudyDescription = "USG ABDOMEN"
    ds.StudyDate = "20260811"
    ds.Laterality = "L"
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    buffer = _io.BytesIO()
    pydicom.dcmwrite(buffer, ds, write_like_original=False)

    parsed = dicom_meta.read_meta(buffer.getvalue())
    check(parsed.patient_name == "Devi Sunita", f"name parsed ({parsed.patient_name!r})")
    check(parsed.sex == "F" and parsed.age_years == 45, "sex and age parsed")
    check(parsed.modality == "US" and parsed.laterality == "L", "modality and laterality parsed")
    check(parsed.study_date == "2026-08-11", f"study date formatted ({parsed.study_date})")

    try:
        dicom_meta.read_meta(b"this is not dicom at all")
        check(False, "junk bytes were accepted as DICOM")
    except ValueError:
        check(True, "junk bytes are refused with a clear error")
except ImportError:
    print("  skip  pydicom is not installed here - read_meta() untested "
          "(pip install pydicom)")


# --------------------------------------------------------------------------- #
print("\nnotify")
# --------------------------------------------------------------------------- #

alert = notify.build_alert(RECORD)
check("STAT" in alert.subject and "Sunita Devi" in alert.subject,
      f"the subject says what and who ({alert.subject})")
check("Dr. Mehta" in alert.body, "the body addresses the referrer")
check("free fluid" in alert.body, "the finding is named")
check(len(alert.short) <= 320, "the SMS fits")

result = notify.send_email("someone@example.com", alert)
check(not result.ok and "not configured" in result.detail,
      "an unconfigured email send fails loudly")
results = notify.send_alert(RECORD, "+910000000000", ["sms"])
check(len(results) == 1 and not results[0].ok and "not configured" in results[0].detail,
      "an unconfigured SMS send fails loudly")
results = notify.send_alert(RECORD, "x", ["pigeon"])
check(not results[0].ok and "Unknown channel" in results[0].detail,
      "an unknown channel is refused")
check(notify.channels_available() == [], "no channels claimed without configuration")


# --------------------------------------------------------------------------- #
print("\nproviders")
# --------------------------------------------------------------------------- #

active = providers.active()
check(active.name == "gemini", f"gemini is the default provider ({active.name})")
for capability in ("structure", "ocr", "draft", "impression", "prefill",
                   "transcribe", "review"):
    check(active.supports(capability), f"gemini provides {capability}")

import os as _os

_os.environ["AI_PROVIDER"] = "nonexistent-vendor"
try:
    fallen_back = providers.active()
    check(fallen_back.name == "gemini", "an unknown provider falls back to gemini")
    check("nonexistent-vendor" in fallen_back.extras.get("warning", ""),
          "the fallback says why")
finally:
    _os.environ.pop("AI_PROVIDER", None)

custom = providers.Provider(name="hospital-onprem", label="On-prem model")
providers.register(custom)
check("hospital-onprem" in providers.available(), "a third-party provider registers")
check(not custom.supports("prefill"), "missing capabilities read as unsupported")


# --------------------------------------------------------------------------- #
print("\nworkers")
# --------------------------------------------------------------------------- #

job_id = workers.submit("test", lambda a, b: a + b, 2, 3)
job = workers.status(job_id)
check(job is not None, "a submitted job has a status")
job.future.result(timeout=10)
check(workers.status(job_id).status == "done", "the job finishes")
check(workers.result(job_id) == 5, "the result comes back")


def explode():
    raise RuntimeError("deliberate")


fail_id = workers.submit("test", explode)
workers.status(fail_id).future.result(timeout=10)
check(workers.status(fail_id).status == "failed", "a crashing job reads as failed")
try:
    workers.result(fail_id)
    check(False, "a failed job's result did not raise")
except RuntimeError as exc:
    check("deliberate" in str(exc), "the failure carries the original error")

check(workers.status("nope") is None, "an unknown job id is None, not a crash")


# --------------------------------------------------------------------------- #
print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All interop checks passed.")
