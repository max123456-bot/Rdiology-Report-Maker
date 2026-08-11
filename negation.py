"""
Negation safety: no clinical negative may silently become a positive.

"No active pleuropulmonary lesion" turning into "Active pleuropulmonary
lesion" is the single worst thing an AI drafting step can do - worse than
crashing, because it reads plausibly. This module is the tripwire:

    entities(text)          -> every clinical entity with its polarity
    check_draft(src, draft) -> the mismatches, if any
    assert_polarity(...)    -> raises NegationMismatchException on a flip

The engine is NegEx-style and fully deterministic: pre-negation triggers
("no", "without", "negative for"), post-negation triggers ("is not seen",
"not visualized"), a scope that ends at the next conjunction or sentence
boundary, and pseudo-negation phrases ("no significant change") that negate
nothing. No models, no network - it must run on every draft, every time,
in microseconds.

Policy on omissions: a negative entity MISSING from a draft is reported as
an omission (warning), while a negative that became positive is a mismatch
(exception). Dropping "no pneumothorax" from an impression is normal
summarisation; asserting pneumothorax is a patient-safety event.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class NegationMismatchException(RuntimeError):
    """A clinical negative in the source became positive in the draft."""

    def __init__(self, mismatches: list["Mismatch"]):
        self.mismatches = mismatches
        detail = "; ".join(
            f"“{m.entity}” is negated in the source but asserted in the draft"
            for m in mismatches
        )
        super().__init__(f"Negation safety stop: {detail}")


# --------------------------------------------------------------------------- #
# Triggers - NegEx, trimmed to what radiology prose actually uses
# --------------------------------------------------------------------------- #

# Phrases that negate what FOLLOWS them, until the scope ends.
_PRE_TRIGGERS = (
    "no evidence of", "no definite evidence of", "no significant evidence of",
    "no evidence for", "negative for", "no features of", "no signs of",
    "no obvious", "no significant", "no definite", "no focal", "no acute",
    "no", "without", "absent", "free of", "denies", "rules out", "ruled out",
    "excludes", "not associated with", "fails to reveal", "nil",
)

# Phrases that negate what PRECEDES them in the same clause.
_POST_TRIGGERS = (
    "is not seen", "are not seen", "not seen", "is not visualized",
    "is not visualised", "not visualized", "not visualised", "is absent",
    "are absent", "is excluded", "was excluded", "is ruled out",
    "not identified", "not demonstrated", "not appreciated", "not evident",
)

# Look like negations, negate nothing. Checked before pre-triggers.
_PSEUDO = (
    "no significant change", "no significant interval change", "no change",
    "no interval change", "no increase", "no decrease", "no further",
    "not only", "no other", "no new",
)

# The scope of a pre-trigger ends at these, or at the sentence's end.
_SCOPE_BREAKERS = re.compile(
    r"\bbut\b|\bhowever\b|\bexcept\b|\bapart from\b|\balthough\b|\bwhereas\b|"
    r"\bwhich\b|\bwith\b(?!out)|\bshows?\b|\breveals?\b|\bdemonstrates?\b",
    re.I,
)

# What counts as a clinical entity worth guarding. Deliberately the nouns that
# change management - not every word in the sentence.
_ENTITY = re.compile(
    r"\b(pneumothorax|pleural effusions?|effusions?|consolidations?|"
    r"pleuropulmonary lesions?|lesions?|masses|mass|nodules?|calculi|calculus|"
    r"stones?|hydronephrosis|hydroureter|free fluid|ascites|lymphadenopathy|"
    r"fractures?|h(?:a?)emorrhages?|h(?:a?)ematomas?|infarcts?|ischa?emia|"
    r"aneurysms?|thromb(?:us|i|osis)|embol(?:us|i|ism)|metasta(?:sis|ses)|"
    r"tumou?rs?|malignancy|abscess(?:es)?|collections?|obstructions?|"
    r"dilat(?:ation|ations)|stenos(?:is|es)|hernias?|cysts?|polyps?|"
    r"fibroids?|septal defects?|cardiomegaly|hepatomegaly|splenomegaly|"
    r"midline shift|oedema|edema|appendicitis|cholecystitis|pancreatitis|"
    r"diverticulitis|pyelonephritis|osteophytes?|spondylolisthesis|"
    r"disc (?:bulge|protrusion|extrusion|herniation)s?|cord compression|"
    r"vascular abnormalit(?:y|ies)|abnormalit(?:y|ies))\b",
    re.I,
)


@dataclass(frozen=True)
class Entity:
    term: str          # normalised, lowercase, singular-ish
    negated: bool
    sentence: str


@dataclass(frozen=True)
class Mismatch:
    entity: str
    source_sentence: str
    draft_sentence: str


def _normalise(term: str) -> str:
    t = term.lower().strip()
    # crude singularisation so "effusions" and "effusion" compare equal
    for plural, singular in (("masses", "mass"), ("calculi", "calculus"),
                             ("metastases", "metastasis"), ("abscesses", "abscess"),
                             ("stenoses", "stenosis")):
        if t == plural:
            return singular
    if t.endswith("s") and not t.endswith(("sis", "us", "ss")):
        t = t[:-1]
    return t


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.;])\s+|\n+", text or "") if s.strip()]


def _mask_pseudo(sentence: str) -> str:
    lowered = sentence.lower()
    for phrase in _PSEUDO:
        lowered = lowered.replace(phrase, " " * len(phrase))
    return lowered


def entities(text: str) -> list[Entity]:
    """Every guarded entity in the text, with its polarity."""
    out: list[Entity] = []
    for sentence in _sentences(text):
        masked = _mask_pseudo(sentence)
        for match in _ENTITY.finditer(masked):
            negated = _is_negated(masked, match.start(), match.end())
            out.append(Entity(
                term=_normalise(match.group(0)),
                negated=negated,
                sentence=sentence[:200],
            ))
    return out


def _is_negated(sentence: str, start: int, end: int) -> bool:
    before = sentence[:start]
    # A scope breaker between the trigger and the entity cancels the negation:
    # "no effusion but a mass" - mass is positive.
    for trigger in _PRE_TRIGGERS:
        for m in re.finditer(r"\b" + re.escape(trigger) + r"\b", before):
            between = before[m.end():]
            if not _SCOPE_BREAKERS.search(between) and len(between) < 80:
                return True
    after = sentence[end:end + 40]
    for trigger in _POST_TRIGGERS:
        if re.match(r"\W{0,4}" + re.escape(trigger) + r"\b", after):
            return True
    return False


def polarity_map(text: str) -> dict[str, bool]:
    """
    entity -> negated, for comparison. If the same entity appears both ways in
    one text ("no free fluid... free fluid in the pelvis"), positive wins -
    an asserted finding anywhere means the entity is asserted.
    """
    out: dict[str, bool] = {}
    for e in entities(text):
        if e.term in out:
            out[e.term] = out[e.term] and e.negated
        else:
            out[e.term] = e.negated
    return out


def check_draft(source: str, draft: str) -> tuple[list[Mismatch], list[str]]:
    """
    Hold a draft against its source.

    Returns (mismatches, omissions):
      mismatches - negated in the source, ASSERTED in the draft. Never OK.
      omissions  - negated in the source, absent from the draft. Usually fine
                   in an impression; the caller decides whether to surface it.
    """
    source_map = polarity_map(source)
    draft_entities = entities(draft)
    draft_map: dict[str, bool] = {}
    draft_sentence: dict[str, str] = {}
    for e in draft_entities:
        if e.term in draft_map:
            draft_map[e.term] = draft_map[e.term] and e.negated
        else:
            draft_map[e.term] = e.negated
        if not e.negated:
            draft_sentence[e.term] = e.sentence

    mismatches: list[Mismatch] = []
    omissions: list[str] = []
    source_sentences = {e.term: e.sentence for e in entities(source) if e.negated}

    for term, negated in source_map.items():
        if not negated:
            continue  # positives are verify.py's word-loss problem, not ours
        if term not in draft_map:
            omissions.append(term)
        elif not draft_map[term]:
            mismatches.append(Mismatch(
                entity=term,
                source_sentence=source_sentences.get(term, ""),
                draft_sentence=draft_sentence.get(term, ""),
            ))
    return mismatches, omissions


def assert_polarity(source: str, draft: str) -> list[str]:
    """
    The hard stop. Raises NegationMismatchException when a negative flipped.
    Returns the (harmless) omission list otherwise.
    """
    mismatches, omissions = check_draft(source, draft)
    if mismatches:
        raise NegationMismatchException(mismatches)
    return omissions
