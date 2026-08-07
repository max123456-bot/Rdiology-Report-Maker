"""
Checks a radiology report before it leaves the building.

verify.py answers "did the formatter change anything?". This answers a different
question: "is this report safe to send?"

Everything here is deterministic - no AI, no network, no guessing. Each check
exists because it catches a mistake that actually reaches patients:

  * a measurement in the IMPRESSION that appears nowhere in the FINDINGS is the
    classic dictation slip - 4 mm heard as 14 mm - and it is the single most
    dangerous error this system can pass through
  * left/right disagreement between findings and impression
  * a heading with nothing under it, usually a paste that lost a section
  * template text nobody replaced: XXX, TBD, ______
  * uncertainty markers the dictation left behind
  * a negation contradicted elsewhere in the same report

Nothing here blocks a download. A radiologist overrides any of it - the report
is theirs. The job is to make sure nobody sends one of these by accident.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SEVERITY_ORDER = {"critical": 0, "warning": 1, "note": 2}


@dataclass
class Finding:
    severity: str      # critical | warning | note
    title: str
    detail: str = ""
    where: str = ""


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    @property
    def critical(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "critical"]

    @property
    def ok(self) -> bool:
        return not self.findings

    @property
    def safe_to_send(self) -> bool:
        return not self.critical

    def sorted(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: SEVERITY_ORDER.get(f.severity, 9))


# --------------------------------------------------------------------------- #

# A measurement: number plus a unit radiologists actually dictate.
_MEASUREMENT = re.compile(
    r"(\d+(?:\.\d+)?)\s*(mm|cm|m|ml|cc|mls?|litres?|liters?|%|hu|mhz|mg|g|kg|"
    r"weeks?|days?|months?|years?|yrs?)\b",
    re.I,
)
_BARE_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
_UNCERTAIN = re.compile(r"\[\[(.+?)\]\]")
_PLACEHOLDER = re.compile(
    r"\b(x{3,}|tbd|to be decided|to be filled|insert\s+\w+|lorem ipsum|"
    r"\.{4,}|_{3,}|dummy|sample text|placeholder|n/?a\s*$)",
    re.I,
)
_LEFT = re.compile(r"\bleft\b|\blt\b|\bl/?s\b", re.I)
_RIGHT = re.compile(r"\bright\b|\brt\b|\br/?s\b", re.I)

# Findings whose negation and assertion must not both appear.
_NEGATION = re.compile(
    r"\bno\b|\bnot\b|\bwithout\b|\babsent\b|\bnegative for\b|\bfree of\b|\bnil\b",
    re.I,
)

IDENTIFIERS = ("PATIENT NAME", "PATIENT'S NAME", "NAME OF PATIENT")
AGE_KEYS = ("AGE/SEX", "AGE / SEX", "AGE", "SEX", "AGE AND SEX")


def _sections(blocks) -> dict[str, list[str]]:
    """Group block text under the main heading it belongs to."""
    out: dict[str, list[str]] = {}
    current = "(before any heading)"
    for b in blocks:
        if b.kind in ("heading", "heading_inline"):
            current = b.text.rstrip(":").strip().upper()
            out.setdefault(current, [])
            if b.kind == "heading_inline" and b.trailer:
                out[current].append(b.trailer)
            continue
        if b.kind == "title":
            out.setdefault("(title)", []).append(b.text)
            continue
        if b.text.strip():
            out.setdefault(current, []).append(b.text)
    return out


def _measurements(text: str) -> set[str]:
    """Normalised 'value unit' pairs, so '4 mm' and '4mm' compare equal."""
    return {f"{m.group(1)} {m.group(2).lower()}" for m in _MEASUREMENT.finditer(text)}


# --------------------------------------------------------------------------- #
# The checks
# --------------------------------------------------------------------------- #


def check_structure(sections: dict[str, list[str]], report: Report) -> None:
    findings_key = next((k for k in sections if k in ("FINDINGS", "OBSERVATIONS")), None)
    impression_key = next(
        (k for k in sections if k in ("IMPRESSION", "CONCLUSION", "OPINION")), None
    )

    if not impression_key:
        report.findings.append(Finding(
            "critical", "No IMPRESSION",
            "Every report needs an impression - it is the part the referring doctor reads.",
        ))
    elif not any(t.strip() for t in sections[impression_key]):
        report.findings.append(Finding(
            "critical", "IMPRESSION is empty",
            "The heading is there but nothing is under it.", impression_key,
        ))

    if not findings_key:
        report.findings.append(Finding(
            "warning", "No FINDINGS section",
            "The report has no findings heading. Intentional for some short studies.",
        ))
    elif not any(t.strip() for t in sections[findings_key]):
        report.findings.append(Finding(
            "critical", "FINDINGS is empty", "The heading is there but nothing is under it.",
            findings_key,
        ))

    for heading, lines in sections.items():
        if heading.startswith("(") or heading in (findings_key, impression_key):
            continue
        if not any(t.strip() for t in lines):
            report.findings.append(Finding(
                "warning", f"{heading} is empty", "A heading with nothing under it.", heading,
            ))


def check_identifiers(sections: dict[str, list[str]], report: Report) -> None:
    has_name = any(
        k in sections and any(t.strip() for t in sections[k]) for k in IDENTIFIERS
    )
    has_age = any(k in sections and any(t.strip() for t in sections[k]) for k in AGE_KEYS)

    if not has_name:
        report.findings.append(Finding(
            "warning", "No patient name",
            "Nothing identifies whose report this is. Easy to attach to the wrong file.",
        ))
    if not has_age:
        report.findings.append(Finding(
            "note", "No age or sex",
            "Age and sex change what is normal. Worth including.",
        ))


def check_impression_measurements(sections: dict[str, list[str]], report: Report) -> None:
    """
    The important one.

    A measurement stated in the IMPRESSION that appears nowhere in the FINDINGS
    is almost always a slip - a misheard digit, or a number typed from memory.
    It is the error most likely to change what happens to the patient.
    """
    findings_key = next((k for k in sections if k in ("FINDINGS", "OBSERVATIONS")), None)
    impression_key = next(
        (k for k in sections if k in ("IMPRESSION", "CONCLUSION", "OPINION")), None
    )
    if not findings_key or not impression_key:
        return

    body = " ".join(sections[findings_key])
    conclusion = " ".join(sections[impression_key])

    body_measurements = _measurements(body)
    body_numbers = set(_BARE_NUMBER.findall(body))

    for measurement in sorted(_measurements(conclusion)):
        value = measurement.split()[0]
        if measurement in body_measurements:
            continue
        if value in body_numbers:
            report.findings.append(Finding(
                "warning", f"“{measurement}” has a different unit in the findings",
                "The number appears in the findings but with another unit. Check which is right.",
                impression_key,
            ))
        else:
            report.findings.append(Finding(
                "critical", f"“{measurement}” is in the impression but not the findings",
                "A measurement that appears only in the impression is usually a misheard or "
                "mistyped digit. Check it against the images.",
                impression_key,
            ))


def check_laterality(sections: dict[str, list[str]], report: Report) -> None:
    findings_key = next((k for k in sections if k in ("FINDINGS", "OBSERVATIONS")), None)
    impression_key = next(
        (k for k in sections if k in ("IMPRESSION", "CONCLUSION", "OPINION")), None
    )
    if not findings_key or not impression_key:
        return

    body = " ".join(sections[findings_key])
    conclusion = " ".join(sections[impression_key])

    body_sides = {"left": bool(_LEFT.search(body)), "right": bool(_RIGHT.search(body))}
    imp_sides = {"left": bool(_LEFT.search(conclusion)), "right": bool(_RIGHT.search(conclusion))}

    for side in ("left", "right"):
        other = "right" if side == "left" else "left"
        if imp_sides[side] and not body_sides[side] and body_sides[other]:
            report.findings.append(Finding(
                "critical", f"Impression says {side}, findings say {other}",
                "Laterality disagrees between the two sections. One of them is wrong, and "
                "this is the error that leads to the wrong side being treated.",
                impression_key,
            ))


def check_leftovers(blocks, report: Report) -> None:
    for b in blocks:
        text = f"{b.text} {b.trailer}".strip()

        for marker in _UNCERTAIN.findall(text):
            report.findings.append(Finding(
                "critical", "Unconfirmed dictation left in the report",
                f"“{marker}” is still marked uncertain. Confirm it in the Dictate tab or "
                "edit it before sending.",
                b.section or b.kind,
            ))

        hit = _PLACEHOLDER.search(text)
        if hit:
            report.findings.append(Finding(
                "critical", f"Placeholder text: “{hit.group(0).strip()}”",
                "Template text nobody replaced.", b.section or b.kind,
            ))


def check_contradictions(sections: dict[str, list[str]], report: Report) -> None:
    """
    A term negated in one place and asserted in another.

    Deliberately narrow - only clear findings, and only reported as a note,
    because 'no free fluid' in findings and 'free fluid' in a differential is
    perfectly normal prose.
    """
    watch = (
        "hydronephrosis", "calculus", "calculi", "free fluid", "mass", "haemorrhage",
        "hemorrhage", "fracture", "infarct", "metastasis", "metastases", "effusion",
        "pneumothorax", "consolidation", "lymphadenopathy",
    )
    everything = " ".join(t for lines in sections.values() for t in lines)
    sentences = re.split(r"(?<=[.;])\s+", everything)

    for term in watch:
        negated = affirmed = None
        for sentence in sentences:
            if term not in sentence.lower():
                continue
            window = sentence.lower()
            index = window.find(term)
            before = window[max(0, index - 40):index]
            if _NEGATION.search(before):
                negated = negated or sentence.strip()
            else:
                affirmed = affirmed or sentence.strip()
        if negated and affirmed:
            report.findings.append(Finding(
                "note", f"“{term}” is both denied and stated",
                f"Denied: “{negated[:90]}”  ·  Stated: “{affirmed[:90]}”. "
                "Often fine in a differential — worth a glance.",
            ))


def check_impression_length(sections: dict[str, list[str]], report: Report) -> None:
    impression_key = next(
        (k for k in sections if k in ("IMPRESSION", "CONCLUSION", "OPINION")), None
    )
    if not impression_key:
        return
    text = " ".join(sections[impression_key]).strip()
    if 0 < len(text) < 15:
        report.findings.append(Finding(
            "warning", "Impression is very short",
            f"“{text}” — check nothing was cut off.", impression_key,
        ))


# --------------------------------------------------------------------------- #


def validate(blocks) -> Report:
    """Run every check over a finished report."""
    report = Report()
    blocks = list(blocks)
    if not blocks:
        report.findings.append(Finding("critical", "The report is empty", ""))
        return report

    sections = _sections(blocks)

    check_structure(sections, report)
    check_identifiers(sections, report)
    check_impression_measurements(sections, report)
    check_laterality(sections, report)
    check_leftovers(blocks, report)
    check_contradictions(sections, report)
    check_impression_length(sections, report)

    return report


def summary(report: Report) -> str:
    if report.ok:
        return "No issues found."
    counts = {}
    for f in report.findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    parts = [f"{n} {name}" for name, n in
             sorted(counts.items(), key=lambda kv: SEVERITY_ORDER.get(kv[0], 9))]
    return " · ".join(parts)
