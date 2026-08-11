"""
Read a DICOM file's metadata and hold it against the report.

Market systems check the dictation against the study's own metadata - the
side that was scanned, the patient's sex and age, the modality - because the
image knows things the transcript cannot. This is that check, for a `.dcm`
file the user drops next to their report.

Only metadata is read. No pixel data is interpreted here - drawing clinical
conclusions from the image is the radiologist's job (or, explicitly, the AI
pre-read path, which is labelled as such).

Everything returns validate.Finding objects so the app shows one combined
safety panel.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from validate import Finding

_LEFT = re.compile(r"\bleft\b|\blt\b\.?", re.I)
_RIGHT = re.compile(r"\bright\b|\brt\b\.?", re.I)

# Organs whose mention pins the patient's sex.
_FEMALE_ONLY = re.compile(r"\buter(?:us|ine)\b|\bovar(?:y|ies|ian)\b|\bendometri|"
                          r"\bcervix\b|\bcervical os\b|\badnexa|\bvagina|\bgestational sac\b",
                          re.I)
_MALE_ONLY = re.compile(r"\bprostat|\btest(?:is|es|icular)\b|\bscrot|\bseminal vesicle",
                        re.I)

# DICOM Modality codes -> what the report title would call it.
_MODALITY_NAMES = {
    "US": "USG", "CT": "CT", "MR": "MRI", "CR": "X-Ray", "DX": "X-Ray",
    "RF": "Fluoroscopy", "MG": "Mammography", "XA": "Angiography",
    "NM": "Nuclear Medicine", "PT": "PET",
}
_MODALITY_WORDS = {
    "USG": ("usg", "ultrasound", "sonograph", "doppler"),
    "CT": ("ct", "computed tomography", "hrct"),
    "MRI": ("mri", "mr ", "magnetic resonance"),
    "X-Ray": ("x-ray", "xray", "radiograph"),
    "Mammography": ("mammo",),
    "Fluoroscopy": ("barium", "ivp", "fluoro"),
    "Angiography": ("angio",),
    "PET": ("pet",),
    "Nuclear Medicine": ("scintigraph", "nuclear"),
}


@dataclass
class StudyMeta:
    """What the DICOM header says about the study."""

    patient_name: str = ""
    sex: str = ""            # M | F | O | ""
    age_years: int | None = None
    modality: str = ""       # the DICOM code: US, CT, MR...
    study_description: str = ""
    body_part: str = ""
    laterality: str = ""     # L | R | ""
    study_date: str = ""     # YYYY-MM-DD
    institution: str = ""
    problems: list[str] = field(default_factory=list)


def read_meta(data: bytes) -> StudyMeta:
    """Parse the header of one DICOM file. Raises ValueError when not DICOM."""
    import io

    try:
        from pydicom import dcmread
        from pydicom.errors import InvalidDicomError
    except ImportError as exc:
        raise ValueError(
            "Reading DICOM needs the pydicom package:  pip install pydicom"
        ) from exc

    try:
        ds = dcmread(io.BytesIO(data), stop_before_pixels=True)
    except InvalidDicomError as exc:
        raise ValueError("That file is not a DICOM file.") from exc
    except Exception as exc:
        raise ValueError(f"Could not read the DICOM file: {exc}") from exc

    meta = StudyMeta()
    meta.patient_name = str(getattr(ds, "PatientName", "") or "").replace("^", " ").strip()
    meta.sex = str(getattr(ds, "PatientSex", "") or "").strip().upper()[:1]
    meta.modality = str(getattr(ds, "Modality", "") or "").strip().upper()
    meta.study_description = str(getattr(ds, "StudyDescription", "") or "").strip()
    meta.body_part = str(getattr(ds, "BodyPartExamined", "") or "").strip()
    meta.institution = str(getattr(ds, "InstitutionName", "") or "").strip()

    laterality = str(
        getattr(ds, "Laterality", "") or getattr(ds, "ImageLaterality", "") or ""
    ).strip().upper()
    if laterality in ("L", "R"):
        meta.laterality = laterality

    raw_age = str(getattr(ds, "PatientAge", "") or "").strip().upper()
    m = re.match(r"^(\d+)([DWMY])$", raw_age)
    if m:
        value, unit = int(m.group(1)), m.group(2)
        meta.age_years = {"D": 0, "W": 0, "M": value // 12}.get(unit, value)
    raw_date = str(getattr(ds, "StudyDate", "") or "").strip()
    if re.match(r"^\d{8}$", raw_date):
        meta.study_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
    return meta


def _name_tokens(name: str) -> set[str]:
    cleaned = re.sub(r"\b(mrs?|ms|miss|dr|master|baby)\b\.?", " ", (name or "").lower())
    return {t for t in re.split(r"[^a-z]+", cleaned) if len(t) > 1}


def cross_check(meta: StudyMeta, report_text: str,
                report_patient: str = "", report_age_sex: str = "") -> list[Finding]:
    """
    Hold the report against the DICOM header. Findings, not verdicts - the
    radiologist may be reporting a different series, or the header may be
    wrong. Every finding says which side of the disagreement came from where.
    """
    findings: list[Finding] = []
    text = report_text or ""

    # Laterality - the classic wrong-side error.
    if meta.laterality:
        scanned = "left" if meta.laterality == "L" else "right"
        other = "right" if scanned == "left" else "left"
        says_other = bool((_RIGHT if scanned == "left" else _LEFT).search(text))
        says_scanned = bool((_LEFT if scanned == "left" else _RIGHT).search(text))
        if says_other and not says_scanned:
            findings.append(Finding(
                "critical",
                f"The scan is of the {scanned} side, the report only says {other}",
                f"DICOM Laterality is “{meta.laterality}” but the report text mentions "
                f"only “{other}”. One of them is the wrong side.",
                "DICOM",
            ))

    # Sex vs gendered anatomy.
    if meta.sex == "M" and _FEMALE_ONLY.search(text):
        hit = _FEMALE_ONLY.search(text).group(0)
        findings.append(Finding(
            "critical", f"Male patient, but the report describes “{hit}”",
            "The DICOM header says the patient is male. Either the header is wrong "
            "or this is the wrong patient's report.", "DICOM",
        ))
    if meta.sex == "F" and _MALE_ONLY.search(text):
        hit = _MALE_ONLY.search(text).group(0)
        findings.append(Finding(
            "critical", f"Female patient, but the report describes “{hit}”",
            "The DICOM header says the patient is female. Either the header is wrong "
            "or this is the wrong patient's report.", "DICOM",
        ))

    # Patient name - token overlap, not equality; order and titles vary.
    if meta.patient_name and report_patient:
        dicom_tokens = _name_tokens(meta.patient_name)
        report_tokens = _name_tokens(report_patient)
        if dicom_tokens and report_tokens and not (dicom_tokens & report_tokens):
            findings.append(Finding(
                "critical", "The report and the scan name different patients",
                f"DICOM says “{meta.patient_name}”, the report says “{report_patient}”. "
                "Attaching a report to the wrong patient's study is unrecoverable - "
                "check before anything else.", "DICOM",
            ))

    # Age, when both sides state one.
    if meta.age_years is not None and report_age_sex:
        m = re.search(r"(\d{1,3})\s*(?:y(?:ea)?rs?|y\b)", report_age_sex.lower())
        if m and abs(int(m.group(1)) - meta.age_years) > 2:
            findings.append(Finding(
                "warning",
                f"Age differs: scan says {meta.age_years}, report says {m.group(1)}",
                "More than two years apart. Worth checking this is the right study.",
                "DICOM",
            ))

    # Modality: warn only when the report CLAIMS a different modality. A report
    # that never names one (or whose title is missing) proves nothing.
    friendly = _MODALITY_NAMES.get(meta.modality, "")
    if friendly:
        head = text[:400].lower()
        # Word boundaries, not substrings - "obstructed" contains "ct".
        claimed = {
            name for name, words in _MODALITY_WORDS.items()
            if any(re.search(r"\b" + re.escape(w.strip()) + r"\b", head)
                   for w in words)
        }
        if claimed and friendly not in claimed:
            findings.append(Finding(
                "warning",
                f"The scan is {friendly} ({meta.modality}), the report reads like "
                f"{', '.join(sorted(claimed))}",
                f"DICOM modality is “{meta.modality}”. If the report is for a different "
                "study, this is the wrong file.", "DICOM",
            ))

    return findings


def describe(meta: StudyMeta) -> str:
    """One line for the UI."""
    bits = []
    if meta.patient_name:
        bits.append(meta.patient_name)
    if meta.sex:
        bits.append(meta.sex)
    if meta.age_years is not None:
        bits.append(f"{meta.age_years}y")
    if meta.modality:
        bits.append(meta.modality)
    if meta.study_description:
        bits.append(meta.study_description)
    if meta.laterality:
        bits.append(f"laterality {meta.laterality}")
    if meta.study_date:
        bits.append(meta.study_date)
    return " · ".join(bits) or "No readable metadata."
