"""
Auto-impression: propose IMPRESSION bullets from the FINDINGS.

Market systems draft the impression the moment the findings are dictated.
Two engines here, used in that order:

  * propose_from_findings() - deterministic, offline. Picks the abnormal
    sentences out of the findings and compresses them into bullet candidates.
    Never invents a word: every proposed bullet is a sentence (or a trimmed
    sentence) that already exists in the findings.
  * ai_parser.draft_impression() - the Gemini path, phrased in the doctor's
    own style. Used when a key is configured; the rule-based result is the
    fallback and the comparison baseline.

Proposals only. The radiologist reviews, edits, accepts or ignores. Nothing
is written into the report without a click.
"""

from __future__ import annotations

import re

# A sentence is "normal prose" when it only says something is fine. These
# sentences never belong in an impression.
_NORMAL = re.compile(
    r"\bnormal\b|\bunremarkable\b|\bpreserved\b|\bwithin normal limits\b|\bwnl\b|"
    r"\bno (?:evidence|significant|obvious|focal|acute)\b|\bare normal\b|"
    r"\bis normal\b|\bappears? normal\b|\bmaintained\b|\bpatent\b|\bclear\b|"
    r"\bno abnormalit|\bnot dilated\b|\bnot enlarged\b|\bnot seen\b|\bnot visuali[sz]ed\b",
    re.I,
)

# Words that mean the sentence carries a finding worth concluding on.
_ABNORMAL = re.compile(
    r"\bmass\b|\blesion\b|\bnodules?\b|\bcysts?\b|\bcalcul(?:us|i)\b|\bstones?\b|"
    r"\bhydronephrosis\b|\bhydroureter\b|\bfatty\b|\bhepatomegaly\b|\bsplenomegaly\b|"
    r"\bcardiomegaly\b|\beffusions?\b|\bascites\b|\bfree fluid\b|\bfractures?\b|"
    r"\boedema\b|\bedema\b|\bthicken|\bdilat|\bstenos|\bhernia|\blymphadenopathy\b|"
    r"\bconsolidation\b|\bopacit|\binfiltrat|\bfibrosis\b|\bscarring\b|\batelecta|"
    r"\bpneumothorax\b|\bh(?:a?)emorrhage\b|\bh(?:a?)ematoma\b|\binfarct|\bischa?emi|"
    r"\baneurysm\b|\bthrombos|\bthrombus\b|\bembol|\bmetasta|\btumou?r\b|\bmalignan|"
    r"\babscess\b|\bcollection\b|\bdiverticul|\bappendicitis\b|\bcholecystitis\b|"
    r"\bpancreatitis\b|\bgallstones?\b|\bcholelithiasis\b|\bnephrolithiasis\b|"
    r"\burolithiasis\b|\bspondylo|\bdisc bulge\b|\bdisc protrusion\b|\bdisc extrusion\b|"
    r"\bstricture\b|\bvarices\b|\bvaricocele\b|\bhydrocele\b|\bpolyps?\b|\bfibroids?\b|"
    r"\bmyoma\b|\badenomyosis\b|\bendometri|\bprostatomegaly\b|\bgrade\b|"
    r"\bsuggestive of\b|\bconsistent with\b|\bin keeping with\b|\blikely\b|"
    r"\bsuspicious\b|\be/o\b|\bs/o\b|\bappears? enlarged\b|\benlarged\b|\bbulky\b",
    re.I,
)

# Hedged normals - "no obvious mass" contains "mass" but asserts nothing.
_NEGATION_BEFORE = re.compile(
    r"\bno\b|\bnot\b|\bwithout\b|\babsent\b|\bnegative for\b|\bfree of\b|\bnil\b|"
    r"\bno evidence of\b|\bruled? out\b",
    re.I,
)


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.;])\s+|\n+", text or "")
    return [p.strip().strip("•-–* ").strip() for p in parts if p.strip().strip("•-–* ").strip()]


def _is_abnormal(sentence: str) -> bool:
    match = _ABNORMAL.search(sentence)
    if not match:
        return False
    before = sentence[max(0, match.start() - 50):match.start()]
    if _NEGATION_BEFORE.search(before):
        # The first cue is negated; is any other cue asserted?
        for other in _ABNORMAL.finditer(sentence, match.end()):
            window = sentence[max(0, other.start() - 50):other.start()]
            if not _NEGATION_BEFORE.search(window):
                return True
        return False
    return True


def propose_from_findings(findings_text: str, limit: int = 8) -> list[str]:
    """
    Bullet candidates for the impression, straight from the findings text.

    Every returned string is a sentence that exists in the findings - trimmed
    of bullet glyphs and trailing punctuation, but with every clinical word,
    number and unit untouched.
    """
    proposals: list[str] = []
    seen: set[str] = set()
    for sentence in _sentences(findings_text):
        if len(sentence) < 8:
            continue
        if _NORMAL.search(sentence) and not _is_abnormal(sentence):
            continue
        if not _is_abnormal(sentence):
            continue
        cleaned = sentence.rstrip(".;")
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        proposals.append(cleaned)
        if len(proposals) >= limit:
            break
    return proposals


def normal_study_line(findings_text: str) -> str | None:
    """
    When nothing abnormal is found, the impression is one line. Returns that
    line, or None when there are abnormal findings.
    """
    if propose_from_findings(findings_text):
        return None
    if not (findings_text or "").strip():
        return None
    return "No significant abnormality detected in the present study."


def as_impression_block(proposals: list[str]) -> str:
    """Render proposals as the text that would sit under IMPRESSION:."""
    if not proposals:
        return ""
    return "\n".join(f"- {p}." for p in proposals)
