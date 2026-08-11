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
# Unfilled template brackets: [INSERT MEASUREMENT], [x], [ ], [...], [value].
# Double brackets are the dictation's own uncertainty markers - handled by
# _UNCERTAIN above, so they are excluded here.
_BRACKET_PLACEHOLDER = re.compile(
    r"(?<!\[)\[\s*(?:insert[^\]]*|x|\.{2,}|_{1,}|value|blank|measurement|"
    r"size|date|name)?\s*\](?!\])",
    re.I,
)
# ALL-CAPS bracket placeholders - the shape the diagnosis drafting mode emits
# on purpose ([RIGHT KIDNEY LENGTH cm], [GRADE]) so a template draft cannot
# be signed until every one is replaced with the patient's own value.
_CAPS_PLACEHOLDER = re.compile(r"(?<!\[)\[[A-Z]{2,}[^\]\n]{0,60}\](?!\])")
_LEFT = re.compile(r"\bleft\b|\blt\b|\bl/?s\b", re.I)
_RIGHT = re.compile(r"\bright\b|\brt\b|\br/?s\b", re.I)

# ---------------------------------------------------------------------------
# Clinical context: current finding, or something else?
#
# "History of left nephrectomy; current right renal mass" is a VALID report.
# A checker that reads both sides out of it and cries "laterality mismatch"
# teaches radiologists to ignore the checker. Each sentence is classified
# before any side is counted, and only CURRENT findings count.
# ---------------------------------------------------------------------------

_HISTORICAL = re.compile(
    r"\bhistory of\b|\bh/o\b|\bknown case of\b|\bk/c/o\b|\bknown\b|"
    r"\bpost[- ]?op(?:erative)?\b|\bs/p\b|\bstatus post\b|\boperated\b|"
    r"\bprevious(?:ly)?\b|\bprior\b|\bold\b|\bresolved\b|\btreated\b|"
    r"\bpost[- ](?:nephrectomy|mastectomy|hysterectomy|cholecystectomy|surgery)\b|"
    r"\bfollow[- ]?up (?:case |study )?(?:of|for)\b",
    re.I,
)
_DIFFERENTIAL = re.compile(
    r"\bdifferentials?\b|\bpossibilit(?:y|ies)\b|\bconsider\b|\bversus\b|\bvs\.?\b|"
    r"\bmay represent\b|\bcould represent\b|\bto be (?:considered|excluded)\b|"
    r"\bd/d\b|\bddx\b",
    re.I,
)
_FAMILY = re.compile(r"\bfamily history\b|\bf/h\b", re.I)


def sentence_context(sentence: str) -> str:
    """current | historical | differential | family - for one sentence."""
    if _FAMILY.search(sentence):
        return "family"
    if _HISTORICAL.search(sentence):
        return "historical"
    if _DIFFERENTIAL.search(sentence):
        return "differential"
    return "current"


def current_sentences(text: str) -> list[str]:
    """Only the sentences that state a present-day finding."""
    parts = re.split(r"(?<=[.;])\s+|\n+", text or "")
    return [p.strip() for p in parts
            if p.strip() and sentence_context(p) == "current"]

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


# Unit families where a magnitude can be expressed two ways. Everything is
# scaled to the family's base so "1.2 cm" and "12 mm" hash identically.
_UNIT_SCALE = {
    "mm": ("len", 1.0), "cm": ("len", 10.0), "m": ("len", 1000.0),
    "ml": ("vol", 1.0), "cc": ("vol", 1.0), "mls": ("vol", 1.0),
    "litres": ("vol", 1000.0), "liters": ("vol", 1000.0),
    "litre": ("vol", 1000.0), "liter": ("vol", 1000.0),
}


def _canonical(measurement: str) -> str:
    """'1.2 cm' -> 'len:12.0'. Units with no family stay as written."""
    try:
        value, unit = measurement.split()
        family, scale = _UNIT_SCALE[unit.lower()]
        return f"{family}:{round(float(value) * scale, 3)}"
    except (ValueError, KeyError):
        return measurement


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
    # The same length in equivalent units: 12 mm IS 1.2 cm, and flagging it
    # taught users to ignore the checker. Both stores are normalised to a
    # canonical magnitude before comparing. 1 ml and 1 cc are also equal.
    body_canonical = {_canonical(m) for m in body_measurements}

    for measurement in sorted(_measurements(conclusion)):
        value = measurement.split()[0]
        if measurement in body_measurements:
            continue
        if _canonical(measurement) in body_canonical:
            continue  # "1.2 cm" in the impression, "12 mm" in the findings - equal
        if value in body_numbers:
            report.findings.append(Finding(
                "warning", f"“{measurement}” has a different unit in the findings",
                "The number appears in the findings but with another unit, and the "
                "magnitudes do not match. Check which is right.",
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
    """
    Context-aware: only CURRENT findings count. "History of left nephrectomy;
    current right renal mass" must pass - the left is history, not a finding.
    """
    findings_key = next((k for k in sections if k in ("FINDINGS", "OBSERVATIONS")), None)
    impression_key = next(
        (k for k in sections if k in ("IMPRESSION", "CONCLUSION", "OPINION")), None
    )
    if not findings_key or not impression_key:
        return

    body = " ".join(current_sentences(" ".join(sections[findings_key])))
    conclusion = " ".join(current_sentences(" ".join(sections[impression_key])))

    body_sides = {"left": bool(_LEFT.search(body)), "right": bool(_RIGHT.search(body))}
    imp_sides = {"left": bool(_LEFT.search(conclusion)), "right": bool(_RIGHT.search(conclusion))}

    for side in ("left", "right"):
        other = "right" if side == "left" else "left"
        if imp_sides[side] and not body_sides[side] and body_sides[other]:
            report.findings.append(Finding(
                "critical", f"Impression says {side}, findings say {other}",
                "Laterality disagrees between the two sections (historical and "
                "differential mentions were excluded before comparing). One side is "
                "wrong, and this is the error that leads to the wrong side being "
                "treated.",
                impression_key,
            ))


# Words that mean the doctor corrected themselves MID-DICTATION and the
# correction was transcribed along with the mistake. "Right kidney shows a
# 12 mm mass, wait, left kidney..." - no parser can decide which half was
# meant, and guessing is the one thing this system never does. The human
# reviews; that is the whole flag.
_SELF_CORRECTION = re.compile(
    r"\bwait\b|\bsorry\b|\bcorrection\b|\bscratch that\b|\bcancel that\b|"
    r"\bignore that\b|\bdelete that\b|\bi mean\b|\bactually no\b|\bno no\b|"
    r"\bstrike that\b|\brather\b,",
    re.I,
)


def check_self_corrections(blocks, report: Report) -> None:
    for b in blocks:
        text = f"{b.text} {b.trailer}".strip()
        hit = _SELF_CORRECTION.search(text)
        if hit:
            report.findings.append(Finding(
                "critical",
                f"Dictation self-correction left in the text: “{hit.group(0).strip()}”",
                "The doctor corrected themselves mid-sentence and both halves were "
                "transcribed. No software can safely decide which half was meant - "
                "edit the line to say only the corrected version.",
                b.section or b.kind,
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

        bracket = _BRACKET_PLACEHOLDER.search(text) or _CAPS_PLACEHOLDER.search(text)
        if bracket:
            report.findings.append(Finding(
                "critical", f"Unfilled template bracket: “{bracket.group(0).strip()}”",
                "A placeholder was never replaced with the patient's own value.",
                b.section or b.kind,
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
# Modality rules from rules_schema.yaml
# --------------------------------------------------------------------------- #

_RULES_PATH = None  # overridable for tests
_rules_cache: dict | None = None

_MODALITY_HINTS = (
    ("hrct", "CT"), ("ct", "CT"), ("computed tomography", "CT"),
    ("mri", "MRI"), ("magnetic resonance", "MRI"),
    ("usg", "USG"), ("ultrasound", "USG"), ("sonograph", "USG"), ("doppler", "USG"),
    ("x-ray", "X-Ray"), ("xray", "X-Ray"), ("radiograph", "X-Ray"),
    ("mammo", "Mammography"),
)


def _modality(title: str) -> str:
    lowered = f" {(title or '').lower()} "
    for needle, name in _MODALITY_HINTS:
        if re.search(r"\b" + re.escape(needle), lowered):
            return name
    return ""


def _load_rules() -> dict:
    """
    rules_schema.yaml, cached. A missing file or a missing yaml package means
    no extra rules - never a crash: the deterministic checks above are the
    safety net and they need nothing.
    """
    global _rules_cache
    if _rules_cache is not None:
        return _rules_cache
    import os

    path = _RULES_PATH or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "rules_schema.yaml"
    )
    try:
        import yaml

        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        _rules_cache = data if isinstance(data, dict) else {}
    except Exception:
        _rules_cache = {}
    return _rules_cache


def reload_rules(path: str | None = None) -> None:
    """Point at another rules file (tests) or pick up an edit."""
    global _RULES_PATH, _rules_cache
    _RULES_PATH = path
    _rules_cache = None


def check_modality_rules(sections: dict[str, list[str]], report: Report) -> None:
    """Apply the YAML-declared rules for this report's modality."""
    rules = _load_rules()
    if not rules:
        return
    title = " ".join(sections.get("(title)", []))
    everything = " ".join(t for lines in sections.values() for t in lines)
    modality = _modality(title)

    layers = [rules.get("default") or {}]
    if modality:
        layers.append((rules.get("modalities") or {}).get(modality) or {})

    for layer in layers:
        severity = str(layer.get("severity") or "note")
        why = str(layer.get("why") or "")

        for heading in layer.get("required_sections") or []:
            key = str(heading).upper()
            if key not in sections or not any(t.strip() for t in sections[key]):
                report.findings.append(Finding(
                    "warning" if severity == "note" else severity,
                    f"{key} is required for {modality or 'this'} reports",
                    why or "Declared required in rules_schema.yaml.", key,
                ))
        for heading in layer.get("recommended_sections") or []:
            key = str(heading).upper()
            if key not in sections or not any(t.strip() for t in sections[key]):
                report.findings.append(Finding(
                    severity, f"{key} is usually present in {modality or 'these'} reports",
                    why or "Recommended in rules_schema.yaml.", key,
                ))
        for item in layer.get("required_phrases") or []:
            phrase = str((item or {}).get("phrase") or "")
            if phrase and phrase.lower() not in everything.lower():
                report.findings.append(Finding(
                    str(item.get("severity") or severity),
                    f"“{phrase}” is expected in {modality or 'this'} report",
                    str(item.get("why") or ""), "(rules)",
                ))
        for item in layer.get("banned_phrases") or []:
            phrase = str((item or {}).get("phrase") or "")
            if phrase and re.search(
                r"\b" + re.escape(phrase) + r"\b\s*\.?\s*$",
                everything.strip(), re.I,
            ):
                report.findings.append(Finding(
                    str(item.get("severity") or severity),
                    f"“{phrase}” with nothing after it",
                    str(item.get("why") or ""), "(rules)",
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
    check_self_corrections(blocks, report)
    check_contradictions(sections, report)
    check_impression_length(sections, report)
    check_modality_rules(sections, report)

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
