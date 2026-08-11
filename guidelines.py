"""
Consensus-guideline advice for a finished report.

Market leaders detect a clinical trigger in the dictation - "6 mm pulmonary
nodule", "thyroid nodule", "breast mass" - and offer the matching consensus
recommendation (Fleischner, BI-RADS, TI-RADS...) for insertion. This is that
engine, deterministic and offline like validate.py.

Two kinds of advice come out:

  * follow-up      - a Fleischner 2017 recommendation sized from the actual
                     measurement in the sentence
  * score-missing  - a scoring system the report should quote but does not
                     (a breast mass with no BI-RADS category, a thyroid
                     nodule with no TI-RADS level, ...)

Nothing is ever inserted automatically. The app shows each advice with an
"insert" button; the radiologist decides. Auto-writing clinical text would
break the first rule of this project - the formatter never invents words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #


@dataclass
class Advice:
    system: str          # "Fleischner 2017", "ACR BI-RADS", ...
    kind: str            # follow-up | score-missing
    trigger: str         # the sentence that fired
    recommendation: str  # text ready to insert under RECOMMENDATION
    detail: str = ""     # why this fired, for the UI


# --------------------------------------------------------------------------- #
# Shared text helpers
# --------------------------------------------------------------------------- #

_NEGATED = re.compile(
    r"\bno\b|\bnot\b|\bwithout\b|\babsent\b|\bnegative for\b|\bfree of\b|\bnil\b|"
    r"\bruled? out\b|\bresolved\b",
    re.I,
)

_MM = re.compile(r"(\d+(?:\.\d+)?)\s*(mm|cm)\b", re.I)

_READ_SECTIONS = {"FINDINGS", "OBSERVATIONS", "IMPRESSION", "CONCLUSION", "OPINION"}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.;\n])\s+", text) if s.strip()]


def _negated(sentence: str, index: int) -> bool:
    return bool(_NEGATED.search(sentence[max(0, index - 60):index]))


def _size_mm(sentence: str) -> float | None:
    """Largest measurement in the sentence, in millimetres."""
    best = None
    for m in _MM.finditer(sentence):
        value = float(m.group(1))
        if m.group(2).lower() == "cm":
            value *= 10
        if best is None or value > best:
            best = value
    return best


def _clinical_text(blocks) -> tuple[str, str]:
    """(title, findings+impression text) from parsed blocks."""
    title = ""
    lines: list[str] = []
    current = ""
    for b in blocks:
        if b.kind == "title":
            title = b.text
            continue
        if b.kind in ("heading", "heading_inline"):
            current = b.text.rstrip(":").strip().upper()
            if b.kind == "heading_inline" and b.trailer and current in _READ_SECTIONS:
                lines.append(b.trailer)
            continue
        if current in _READ_SECTIONS and b.text.strip():
            lines.append(b.text)
    return title, "\n".join(lines)


# --------------------------------------------------------------------------- #
# Fleischner 2017 - incidental pulmonary nodules
# --------------------------------------------------------------------------- #

_NODULE = re.compile(r"\bnodules?\b", re.I)
_LUNG_CONTEXT = re.compile(
    r"\blungs?\b|\bpulmonary\b|\blobes?\b|\blingula\b|\bperifissural\b|"
    r"\bsubpleural\b|\bapical\b|\bbasal\b|\bhilar\b|\bbronch",
    re.I,
)
_CHEST_STUDY = re.compile(r"\bchest\b|\bthorax\b|\bhrct\b|\blung\b", re.I)
_SUBSOLID = re.compile(r"ground[- ]?glass|\bggo\b|\bggn\b", re.I)
_PART_SOLID = re.compile(r"part[- ]?solid|semi[- ]?solid", re.I)
_MULTIPLE = re.compile(r"\bnodules\b|\bmultiple\b|\bfew\b|\bseveral\b|\bbilateral\b", re.I)

_FLEISCHNER_NOTE = (
    "(Fleischner Society 2017 guideline for incidental pulmonary nodules in adults "
    "aged 35 or older; not applicable to lung cancer screening, immunocompromised "
    "patients, or patients with a known primary cancer.)"
)


def _fleischner(sentence: str, size_mm: float | None) -> str:
    """The 2017 Fleischner recommendation for the nodule in this sentence."""
    part_solid = bool(_PART_SOLID.search(sentence))
    ground_glass = bool(_SUBSOLID.search(sentence)) and not part_solid
    multiple = bool(_MULTIPLE.search(sentence))

    if ground_glass:
        if size_mm is not None and size_mm < 6:
            body = ("Pure ground-glass nodule smaller than 6 mm: no routine follow-up "
                    "required per Fleischner 2017.")
        else:
            body = ("Ground-glass nodule 6 mm or larger: CT chest at 6-12 months to "
                    "confirm persistence, then CT every 2 years until 5 years, per "
                    "Fleischner 2017.")
    elif part_solid:
        body = ("Part-solid nodule: CT chest at 3-6 months to confirm persistence. If "
                "unchanged and the solid component remains below 6 mm, annual CT for 5 "
                "years. A solid component of 6 mm or more is suspicious - consider "
                "PET-CT or tissue sampling, per Fleischner 2017.")
    elif multiple and size_mm is not None and size_mm >= 6:
        body = ("Multiple solid nodules, dominant nodule 6 mm or larger: CT chest at "
                "3-6 months, then consider CT at 18-24 months, per Fleischner 2017.")
    elif size_mm is None:
        body = ("Solid pulmonary nodule, size not stated: follow-up depends on size "
                "per Fleischner 2017 - under 6 mm generally needs no routine follow-up "
                "in low-risk patients; 6-8 mm needs CT at 6-12 months; over 8 mm "
                "warrants CT at 3 months, PET-CT, or tissue sampling. Please state "
                "the nodule size.")
    elif size_mm < 6:
        body = ("Solid pulmonary nodule smaller than 6 mm: no routine follow-up in "
                "low-risk patients; optional CT at 12 months if high risk, per "
                "Fleischner 2017.")
    elif size_mm <= 8:
        body = ("Solid pulmonary nodule of 6-8 mm: CT chest at 6-12 months, then "
                "consider CT at 18-24 months, per Fleischner 2017.")
    else:
        body = ("Solid pulmonary nodule larger than 8 mm: consider CT chest at 3 "
                "months, PET-CT, or tissue sampling, per Fleischner 2017.")
    return f"{body} {_FLEISCHNER_NOTE}"


# --------------------------------------------------------------------------- #
# Scoring systems the report should quote
# --------------------------------------------------------------------------- #

# (system, already-scored pattern, trigger pattern, context pattern or None,
#  recommendation text)
_SCORING: tuple[tuple[str, re.Pattern, re.Pattern, re.Pattern | None, str], ...] = (
    (
        "ACR BI-RADS",
        re.compile(r"\bbi-?rads\b", re.I),
        re.compile(r"\b(mass|lesion|lump|asymmetry|calcifications?)\b", re.I),
        re.compile(r"\bbreasts?\b|\bmammo|\baxilla", re.I),
        "Suggest assigning a BI-RADS assessment category (ACR BI-RADS Atlas, 5th "
        "edition) to this breast finding, with the matching management "
        "recommendation.",
    ),
    (
        "ACR TI-RADS",
        re.compile(r"\bti-?rads\b", re.I),
        re.compile(r"\bnodules?\b", re.I),
        re.compile(r"\bthyroid\b|\bisthmus\b", re.I),
        "Suggest scoring this thyroid nodule with ACR TI-RADS (composition, "
        "echogenicity, shape, margin, echogenic foci) and stating whether FNA or "
        "follow-up ultrasound is indicated by the resulting level.",
    ),
    (
        "PI-RADS v2.1",
        re.compile(r"\bpi-?rads\b", re.I),
        re.compile(r"\b(lesion|focus|focal abnormality|nodule)\b", re.I),
        re.compile(r"\bprostat", re.I),
        "Suggest assigning a PI-RADS v2.1 assessment category to this prostate "
        "lesion, stating the sector and the dominant sequence used.",
    ),
    (
        "LI-RADS",
        re.compile(r"\bli-?rads\b", re.I),
        re.compile(r"\b(lesion|mass|nodule|observation)\b", re.I),
        re.compile(r"\bliver\b|\bhepatic\b|\bcirrho|\bhepatitis\b", re.I),
        "Suggest categorising this hepatic observation with LI-RADS (CT/MRI "
        "v2018) given the at-risk liver, stating major features: size, "
        "enhancement pattern, washout, capsule.",
    ),
    (
        "O-RADS",
        re.compile(r"\bo-?rads\b", re.I),
        re.compile(r"\b(cyst|mass|lesion)\b", re.I),
        re.compile(r"\bovar|\badnex", re.I),
        "Suggest assigning an O-RADS category to this adnexal finding, with "
        "the matching management recommendation.",
    ),
    (
        "Bosniak",
        re.compile(r"\bbosniak\b", re.I),
        re.compile(r"\b(cyst|cystic lesion)\b", re.I),
        re.compile(r"\brenal\b|\bkidneys?\b", re.I),
        "Suggest classifying this renal cyst with the Bosniak system (v2019) - "
        "category I/II need no follow-up, IIF needs surveillance, III/IV need "
        "urology referral.",
    ),
)


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def advise_text(text: str, title: str = "") -> list[Advice]:
    """All guideline advice for free text. `title` sharpens study context."""
    advice: list[Advice] = []
    sentences = _sentences(text or "")
    everything = text or ""
    chest_study = bool(_CHEST_STUDY.search(title or ""))

    # Fleischner - once, on the first qualifying nodule sentence.
    for sentence in sentences:
        match = _NODULE.search(sentence)
        if not match or _negated(sentence, match.start()):
            continue
        if _SUBSOLID.search(sentence) or _PART_SOLID.search(sentence) or \
                _LUNG_CONTEXT.search(sentence) or chest_study:
            if re.search(r"\bthyroid\b|\bisthmus\b", sentence, re.I):
                continue  # a thyroid nodule is TI-RADS territory, not Fleischner
            advice.append(Advice(
                system="Fleischner 2017",
                kind="follow-up",
                trigger=sentence[:200],
                recommendation=_fleischner(sentence, _size_mm(sentence)),
                detail="Pulmonary nodule stated without a follow-up recommendation."
                       if "fleischner" not in everything.lower() else "",
            ))
            break
    # Already recommended by the radiologist - drop the advice.
    if advice and "fleischner" in everything.lower():
        advice.clear()

    # Scoring systems.
    for system, scored, trigger, context, recommendation in _SCORING:
        if scored.search(everything):
            continue  # already quoted somewhere in the report
        for sentence in sentences:
            match = trigger.search(sentence)
            if not match or _negated(sentence, match.start()):
                continue
            if context is not None and not context.search(sentence):
                continue
            advice.append(Advice(
                system=system,
                kind="score-missing",
                trigger=sentence[:200],
                recommendation=recommendation,
                detail=f"The finding is stated but no {system} category is quoted.",
            ))
            break

    return advice


def advise_blocks(blocks) -> list[Advice]:
    """Guideline advice for a parsed report - reads findings and impression."""
    title, text = _clinical_text(blocks)
    if not text.strip():
        text = "\n".join(b.text for b in blocks if b.text.strip())
    return advise_text(text, title=title)


def summary(advice: list[Advice]) -> str:
    if not advice:
        return "No guideline triggers."
    return " · ".join(f"{a.system}" for a in advice)
