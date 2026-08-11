"""
Urgency triage for a finished report.

Market RIS platforms bump critical studies to the top of the worklist and fire
an alert to the referring doctor the moment one is signed. This is the engine
behind both: read the clinical text, decide stat / urgent / routine.

Deterministic on purpose - no AI, no network. The same design rule as
validate.py: a triage that guesses is worse than one that is merely careful.
Negated findings ("no pneumothorax") and historical ones ("old infarct") do
not trigger. Only FINDINGS and IMPRESSION text is read when block structure is
available, so "rule out PE" in the clinical history cannot mark a normal study
stat.

A hit never blocks anything. It ranks the worklist and offers an alert; the
radiologist stays in charge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Findings that mean "phone the referrer now". Phrases, longest first, matched
# case-insensitively on word boundaries.
STAT_TERMS: tuple[str, ...] = (
    "tension pneumothorax",
    "intracranial hemorrhage", "intracranial haemorrhage",
    "extradural hemorrhage", "extradural haemorrhage",
    "epidural hemorrhage", "epidural haemorrhage",
    "subdural hemorrhage", "subdural haemorrhage",
    "subdural hematoma", "subdural haematoma",
    "extradural hematoma", "extradural haematoma",
    "epidural hematoma", "epidural haematoma",
    "subarachnoid hemorrhage", "subarachnoid haemorrhage",
    "intraparenchymal hemorrhage", "intraparenchymal haemorrhage",
    "intraventricular hemorrhage", "intraventricular haemorrhage",
    "hemorrhagic transformation", "haemorrhagic transformation",
    "midline shift",
    "uncal herniation", "tonsillar herniation", "brain herniation",
    "pulmonary embolism", "pulmonary embolus", "saddle embolus",
    "aortic dissection", "aortic transection", "aortic rupture",
    "ruptured aneurysm", "leaking aneurysm", "contained rupture",
    "pneumoperitoneum", "free intraperitoneal air", "free air under the diaphragm",
    "free air under diaphragm", "free subdiaphragmatic air",
    "bowel perforation", "hollow viscus perforation",
    "testicular torsion", "ovarian torsion",
    "ectopic pregnancy",
    "cord compression", "cauda equina",
    "unstable fracture", "atlanto-axial subluxation",
    "portal venous gas", "pneumatosis intestinalis",
    "fetal demise", "intrauterine death", "absent fetal cardiac activity",
    "placental abruption",
    "active extravasation", "active contrast extravasation",
)

# Findings that need same-day attention but not a phone call mid-scan.
URGENT_TERMS: tuple[str, ...] = (
    "pneumothorax",
    "acute infarct", "acute ischemic", "acute ischaemic", "acute stroke",
    "acute appendicitis", "appendicitis",
    "intussusception", "volvulus", "small bowel obstruction",
    "large bowel obstruction", "bowel obstruction", "closed loop obstruction",
    "acute cholecystitis", "emphysematous cholecystitis",
    "obstructive hydronephrosis", "obstructive uropathy", "obstructed kidney",
    "pyonephrosis", "emphysematous pyelonephritis",
    "deep vein thrombosis", "deep venous thrombosis", "dural venous sinus thrombosis",
    "cerebral venous thrombosis", "portal vein thrombosis",
    "abscess", "empyema", "necrotizing fasciitis", "necrotising fasciitis",
    "acute hydrocephalus", "obstructive hydrocephalus",
    "pathological fracture", "pathologic fracture",
    "retained foreign body",
    "suspicious for malignancy", "suspicious of malignancy", "highly suspicious",
    "pericardial effusion with tamponade", "cardiac tamponade",
    "retroperitoneal hemorrhage", "retroperitoneal haemorrhage",
    "splenic laceration", "hepatic laceration", "renal laceration",
    "displaced fracture",
    "impending rupture",
)

LEVELS = ("stat", "urgent", "routine")
_LEVEL_RANK = {"stat": 0, "urgent": 1, "routine": 2}

# Words before a term that mean it is absent or historical, not an acute find.
_DISMISS = re.compile(
    r"\bno\b|\bnot\b|\bwithout\b|\babsent\b|\bnegative for\b|\bfree of\b|\bnil\b|"
    r"\bruled? out\b|\brule out\b|\br/o\b|\bexclude[sd]?\b|\bunlikely\b|"
    r"\bresolved\b|\bold\b|\bchronic\b|\bprevious\b|\bprior\b|\bknown\b|"
    r"\bpost[- ]?operative\b|\btreated\b|\bhealed\b|\bquery\b|\bversus\b|\bvs\.?\b",
    re.I,
)

# Sections triage reads when block structure is available. Clinical history and
# indication are deliberately excluded: "k/c/o PE" there is a question, not a
# finding.
_READ_SECTIONS = {
    "FINDINGS", "OBSERVATIONS", "IMPRESSION", "CONCLUSION", "OPINION",
    "COMMENT", "COMMENTS", "RECOMMENDATION", "RECOMMENDATIONS", "ADVICE",
}


@dataclass
class TriageHit:
    term: str
    level: str        # stat | urgent
    sentence: str     # the sentence that triggered, for the UI and the alert


@dataclass
class TriageResult:
    level: str = "routine"
    hits: list[TriageHit] = field(default_factory=list)

    @property
    def is_critical(self) -> bool:
        return self.level == "stat"

    @property
    def needs_alert(self) -> bool:
        return self.level in ("stat", "urgent")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.;\n])\s+", text) if s.strip()]


def _scan(sentences: list[str], terms: tuple[str, ...], level: str,
          result: TriageResult, seen: set[str]) -> None:
    for term in terms:
        if term in seen:
            continue
        pattern = re.compile(r"\b" + re.escape(term) + r"\b", re.I)
        for sentence in sentences:
            match = pattern.search(sentence)
            if not match:
                continue
            before = sentence[max(0, match.start() - 60):match.start()]
            if _DISMISS.search(before):
                continue
            result.hits.append(TriageHit(term=term, level=level, sentence=sentence[:200]))
            seen.add(term)
            break


def triage_text(text: str) -> TriageResult:
    """Triage free text. Used when no block structure exists."""
    result = TriageResult()
    sentences = _sentences(text or "")
    seen: set[str] = set()
    _scan(sentences, STAT_TERMS, "stat", result, seen)
    # A stat phrase containing an urgent word ("tension pneumothorax") must not
    # also fire the urgent word; drop urgent terms already inside a stat hit.
    stat_sentences = {h.sentence for h in result.hits}
    _scan(sentences, URGENT_TERMS, "urgent", result, seen)
    result.hits = [
        h for h in result.hits
        if h.level == "stat" or h.sentence not in stat_sentences
    ]
    if any(h.level == "stat" for h in result.hits):
        result.level = "stat"
    elif result.hits:
        result.level = "urgent"
    return result


def triage_blocks(blocks) -> TriageResult:
    """
    Triage a parsed report, reading only the sections where an assertion means
    the finding is present (findings, impression, recommendations).
    """
    lines: list[str] = []
    current = ""
    for b in blocks:
        if b.kind in ("heading", "heading_inline"):
            current = b.text.rstrip(":").strip().upper()
            if b.kind == "heading_inline" and b.trailer and current in _READ_SECTIONS:
                lines.append(b.trailer)
            continue
        if b.kind == "title":
            continue
        if current in _READ_SECTIONS and b.text.strip():
            lines.append(b.text)
    if not lines:  # no recognised sections - fall back to everything
        lines = [b.text for b in blocks if b.text.strip()]
    return triage_text("\n".join(lines))


def worse(a: str, b: str) -> str:
    """The more urgent of two levels."""
    return a if _LEVEL_RANK.get(a, 2) <= _LEVEL_RANK.get(b, 2) else b


def summary(result: TriageResult) -> str:
    if not result.hits:
        return "Routine - no critical finding detected."
    terms = ", ".join(sorted({h.term for h in result.hits}))
    return f"{result.level.upper()} - {terms}"
