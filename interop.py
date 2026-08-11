"""
Hospital-facing exports: HL7 v2.5 ORU^R01 and FHIR R4 DiagnosticReport.

A .docx is what the clinic prints; HL7 and FHIR are what a RIS, an EHR or an
integration engine ingests. Both builders work from a report record (see
records.py) and are pure functions - offline, deterministic, testable.

HL7 v2.5, not FHIR alone, because Indian hospital integration engines
(Mirth and friends) still speak v2 almost exclusively. The FHIR bundle is
for the systems that have moved on.

Nothing here transmits. The app offers both as downloads; wiring a real MLLP
or REST endpoint is deployment configuration, done with the receiving
hospital's integration team.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import datetime, timezone

SENDING_APP = "HCFORMAT"

# --------------------------------------------------------------------------- #
# Shared field helpers
# --------------------------------------------------------------------------- #


def sex_of(age_sex: str) -> str:
    """M/F/O out of the free-text AGE/SEX line, or ''."""
    lowered = (age_sex or "").lower()
    if re.search(r"\bf(?:emale)?\b", lowered):
        return "F"
    if re.search(r"\bm(?:ale)?\b", lowered):
        return "M"
    return ""


def hl7_name(name: str) -> str:
    """
    'Mrs. Sunita Devi' -> 'DEVI^SUNITA' - family name first, per PID-5.

    Each component is escaped here, because the ^ this builds is a real
    component separator and must NOT be escaped by the caller.
    """
    cleaned = re.sub(r"\b(mrs?|ms|miss|dr|master|baby)\b\.?", "", name or "",
                     flags=re.I).strip()
    tokens = [t for t in cleaned.split() if t]
    if not tokens:
        return ""
    if len(tokens) == 1:
        return hl7_escape(tokens[0].upper())
    return (f"{hl7_escape(tokens[-1].upper())}^"
            f"{hl7_escape(' '.join(tokens[:-1]).upper())}")


def impression_of(report_text: str) -> str:
    """The text under IMPRESSION/CONCLUSION/OPINION, for OBX-5 and conclusion."""
    lines = (report_text or "").splitlines()
    out: list[str] = []
    inside = False
    heading = re.compile(r"^\s*([A-Z][A-Z /'&-]+?)\s*:?\s*$")
    starts = ("IMPRESSION", "CONCLUSION", "OPINION")
    for line in lines:
        m = heading.match(line.strip())
        if m:
            inside = m.group(1).strip().upper().rstrip(":") in starts
            continue
        if inside and line.strip():
            out.append(line.strip())
    return "\n".join(out)


def _ts(iso: str = "") -> str:
    """HL7 DTM: YYYYMMDDHHMMSS."""
    try:
        when = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except Exception:
        when = datetime.now(timezone.utc)
    return when.strftime("%Y%m%d%H%M%S")


# --------------------------------------------------------------------------- #
# HL7 v2.5 ORU^R01
# --------------------------------------------------------------------------- #

_HL7_ESCAPES = (
    ("\\", r"\E\ "), ("|", r"\F\ "), ("^", r"\S\ "), ("~", r"\R\ "), ("&", r"\T\ "),
)


def hl7_escape(value: str) -> str:
    out = str(value or "")
    for char, escape in _HL7_ESCAPES:
        out = out.replace(char, escape.strip())
    return out.replace("\r", " ").replace("\n", " ")


def hl7_oru(record: dict, facility: str = "", receiving_app: str = "RIS",
            receiving_facility: str = "HOSPITAL") -> str:
    """
    One ORU^R01 message for a signed report.

    Segments: MSH, PID, OBR, then one TX OBX per line of the report - the
    whole audited text, not a summary, because the receiving system's copy
    must match the .docx word for word.
    """
    now = _ts()
    control_id = record.get("id") or uuid.uuid4().hex[:12]
    text = record.get("report_text") or ""
    study = record.get("study") or "RADIOLOGY REPORT"
    urgency_flag = "S" if record.get("urgency") == "stat" else "R"

    segments: list[str] = [
        "MSH|^~\\&|" + "|".join([
            SENDING_APP,
            hl7_escape(facility or record.get("tenant", "") or "CLINIC"),
            receiving_app,
            receiving_facility,
            now,
            "",
            "ORU^R01^ORU_R01",
            control_id,
            "P",
            "2.5",
        ]),
        # PID-3 empty (no MRN in this system yet), PID-5 name, PID-7 DOB
        # (reports carry age, not DOB), PID-8 sex. hl7_name escapes its own
        # components - escaping again would mangle the ^ separator.
        "|".join([
            "PID", "1", "", "", "",
            hl7_name(record.get("patient", "")),
            "", "",
            sex_of(record.get("age_sex", "")),
        ]),
        "OBR|1||" + "|".join([
            hl7_escape(control_id),
            hl7_escape(f"IMG^{study}"),
            urgency_flag,
            "",
            _ts(record.get("created", "")),
            "", "", "", "", "", "", "", "",
            hl7_escape(record.get("referrer", "")),
            "", "", "", "", "",
            _ts(record.get("signed_at", "") or record.get("updated", "")),
            "", "",
            "F" if record.get("status") in ("signed", "delivered") else "P",
        ]),
    ]

    index = 1
    for line in text.splitlines():
        if not line.strip():
            continue
        segments.append(f"OBX|{index}|TX|59776-5^Procedure findings^LN||"
                        f"{hl7_escape(line.strip())}||||||F")
        index += 1

    conclusion = impression_of(text)
    if conclusion:
        for line in conclusion.splitlines():
            segments.append(f"OBX|{index}|TX|19005-8^Radiology imaging study "
                            f"impression^LN||{hl7_escape(line)}||||||F")
            index += 1

    return "\r".join(segments) + "\r"


# --------------------------------------------------------------------------- #
# FHIR R4
# --------------------------------------------------------------------------- #

_FHIR_STATUS = {"draft": "preliminary", "signed": "final", "delivered": "final"}


def fhir_diagnostic_report(record: dict, docx_bytes: bytes | None = None) -> dict:
    """
    A self-contained FHIR R4 collection Bundle: Patient + DiagnosticReport.

    presentedForm carries the full audited text (and the .docx when given),
    so the receiving system stores exactly what the clinic printed.
    """
    text = record.get("report_text") or ""
    patient_id = f"patient-{record.get('patient_key') or 'unknown'}"[:64]
    report_id = f"report-{record.get('id') or uuid.uuid4().hex[:12]}"

    patient: dict = {
        "resourceType": "Patient",
        "id": patient_id,
    }
    if record.get("patient"):
        patient["name"] = [{"text": record["patient"]}]
    sex = sex_of(record.get("age_sex", ""))
    if sex:
        patient["gender"] = "female" if sex == "F" else "male"

    presented = [{
        "contentType": "text/plain",
        "data": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "title": record.get("study") or "Radiology report",
    }]
    if docx_bytes:
        presented.append({
            "contentType": ("application/vnd.openxmlformats-officedocument"
                            ".wordprocessingml.document"),
            "data": base64.b64encode(docx_bytes).decode("ascii"),
            "title": "Formatted report (.docx)",
        })

    report: dict = {
        "resourceType": "DiagnosticReport",
        "id": report_id,
        "status": _FHIR_STATUS.get(str(record.get("status")), "preliminary"),
        "category": [{
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/v2-0074",
                "code": "RAD",
                "display": "Radiology",
            }],
        }],
        "code": {
            "coding": [{
                "system": "http://loinc.org",
                "code": "18748-4",
                "display": "Diagnostic imaging study",
            }],
            "text": record.get("study") or "Radiology report",
        },
        "subject": {"reference": f"Patient/{patient_id}"},
        "issued": (record.get("signed_at") or record.get("updated")
                   or datetime.now(timezone.utc).isoformat(timespec="seconds")),
        "presentedForm": presented,
    }
    if record.get("created"):
        report["effectiveDateTime"] = record["created"]
    conclusion = impression_of(text)
    if conclusion:
        report["conclusion"] = conclusion
    if record.get("referrer"):
        report["performer"] = []
        report["basedOn"] = [{"display": f"Referred by {record['referrer']}"}]

    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"resource": patient}, {"resource": report}],
    }


def fhir_json(record: dict, docx_bytes: bytes | None = None) -> str:
    return json.dumps(fhir_diagnostic_report(record, docx_bytes), indent=2,
                      ensure_ascii=False)
