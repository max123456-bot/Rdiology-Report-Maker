"""
Hierarchical anatomical parsing: findings as a tree, not a flat list.

"Right lobe of liver shows a hypoechoic lesion" belongs under
ABDOMEN -> LIVER -> RIGHT LOBE, and a system consuming the structured output
(the API, a downstream RIS) wants exactly that shape:

    {"ABDOMEN": {"LIVER": {"RIGHT LOBE": ["Focal hypoechoic lesion ..."],
                           "GENERAL":    ["Normal in size ..."]},
                 "LEFT KIDNEY": {"GENERAL": [...]}}}

Alongside it, the deterministic slice of coreference resolution radiology
prose actually needs: "A cyst is seen in the left kidney. It measures
2.5 cm." - the second sentence names no organ, so it attaches to the organ
(and sub-part) most recently in context. No model, no guessing beyond
"the sentence continues the current topic", which is how dictation works.

The tree is a VIEW. The report text itself is never reordered or rewritten -
hc_format and the word-loss audit remain the only path to the document.
"""

from __future__ import annotations

import re

# Region -> the organs that live there. An organ found in the text files its
# sentences under its region.
REGIONS: dict[str, tuple[str, ...]] = {
    "ABDOMEN": ("liver", "gallbladder", "pancreas", "spleen", "kidney",
                "ureter", "bladder", "appendix", "bowel", "stomach", "aorta",
                "cbd", "common bile duct", "portal vein", "adrenal"),
    "PELVIS": ("uterus", "endometrium", "ovary", "adnexa", "prostate",
               "seminal vesicle", "cervix"),
    "CHEST": ("lung", "pleura", "heart", "mediastinum", "trachea",
              "diaphragm", "rib"),
    "NECK": ("thyroid", "parotid", "submandibular", "lymph node"),
    "BRAIN": ("cerebrum", "cerebellum", "brainstem", "ventricle", "sella",
              "pituitary", "brain parenchyma"),
    "MSK": ("spine", "disc", "vertebra", "shoulder", "knee", "hip",
            "femur", "humerus"),
    "BREAST": ("breast", "axilla"),
    "SCROTUM": ("testis", "epididymis", "scrotum"),
}

# Organ -> its named sub-parts. "right lobe of liver", "upper pole of the
# left kidney" - the phrases radiologists actually dictate.
SUBPARTS: dict[str, tuple[str, ...]] = {
    "liver": ("right lobe", "left lobe", "caudate lobe",
              "segment i", "segment ii", "segment iii", "segment iv",
              "segment v", "segment vi", "segment vii", "segment viii"),
    "lung": ("upper lobe", "middle lobe", "lower lobe", "lingula", "apex",
             "upper zone", "mid zone", "lower zone"),
    "kidney": ("upper pole", "lower pole", "mid pole", "interpolar region",
               "cortex", "pelvis"),
    "thyroid": ("right lobe", "left lobe", "isthmus"),
    "breast": ("upper outer quadrant", "upper inner quadrant",
               "lower outer quadrant", "lower inner quadrant",
               "retroareolar region"),
    "uterus": ("fundus", "body", "cervix", "endometrium", "myometrium"),
    "prostate": ("peripheral zone", "transition zone", "central zone",
                 "median lobe"),
    "spine": ("cervical", "dorsal", "thoracic", "lumbar", "sacral"),
}

_ORGAN_OF: dict[str, str] = {
    organ: region for region, organs in REGIONS.items() for organ in organs
}
_ORGANS = sorted(_ORGAN_OF, key=len, reverse=True)
_ORGAN_RE = re.compile(
    r"\b(?:(right|left|both|bilateral)\s+)?("
    + "|".join(re.escape(o) for o in _ORGANS) + r")s?\b",
    re.I,
)
_SIDE_FIRST = {"rt": "right", "lt": "left"}

# Sentences that continue the previous topic rather than opening a new one.
_CONTINUATION = re.compile(
    r"^\s*(?:it|this|these|they|which|the lesion|the mass|the cyst|"
    r"the nodule|the collection|no internal|there is no)\b",
    re.I,
)

GENERAL = "GENERAL"


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.;])\s+|\n+", text or "")
            if s.strip()]


def _find_subpart(sentence: str, organ: str) -> str | None:
    lowered = sentence.lower()
    for subpart in SUBPARTS.get(organ, ()):
        if subpart in lowered:
            return subpart.upper()
    return None


def _organ_key(side: str, organ: str) -> str:
    side = _SIDE_FIRST.get(side.lower(), side.lower()) if side else ""
    if side in ("both", "bilateral"):
        side = "bilateral"
    return f"{side.upper()} {organ.upper()}".strip()


def findings_tree(findings_text: str) -> dict:
    """
    REGION -> ORGAN (with side) -> SUBPART -> [sentences].

    Sentence attachment rules, in order:
      1. names an organ -> that organ (sub-part when named in the sentence)
      2. continuation phrasing ("It measures...") or no anatomy at all ->
         the most recent organ and sub-part - the deterministic slice of
         coreference resolution
      3. nothing seen yet -> UNASSIGNED, honestly, rather than guessed
    """
    tree: dict = {}
    current: tuple[str, str, str] | None = None  # (region, organ_key, subpart)

    def put(region: str, organ_key: str, subpart: str, sentence: str) -> None:
        tree.setdefault(region, {}).setdefault(organ_key, {}) \
            .setdefault(subpart, []).append(sentence)

    for sentence in _sentences(findings_text):
        match = _ORGAN_RE.search(sentence)
        if match and not (_CONTINUATION.match(sentence) and current):
            side = match.group(1) or ""
            organ = match.group(2).lower()
            region = _ORGAN_OF.get(organ, "OTHER")
            organ_key = _organ_key(side, organ)
            subpart = _find_subpart(sentence, organ) or GENERAL
            current = (region, organ_key, subpart)
            put(region, organ_key, subpart, sentence)
        elif current is not None:
            put(*current, sentence)
        else:
            tree.setdefault("UNASSIGNED", {}).setdefault(GENERAL, {}) \
                .setdefault(GENERAL, []).append(sentence)
    return tree


def flatten(tree: dict) -> list[tuple[str, str]]:
    """[(REGION > ORGAN > SUBPART, sentence)] - for tables and display."""
    out: list[tuple[str, str]] = []
    for region, organs in tree.items():
        for organ_key, subparts in organs.items():
            for subpart, sentences in subparts.items():
                path = " > ".join(p for p in (region, organ_key, subpart)
                                  if p and p != GENERAL) or region
                out.extend((path, s) for s in sentences)
    return out
