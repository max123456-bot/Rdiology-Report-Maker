"""
De-identification before anything leaves for a cloud model.

The model drafting a report does not need to know WHO the patient is - it
needs the findings. So when DEID_CLOUD is on, every text payload bound for a
cloud AI first has its direct identifiers swapped for stable placeholders,
and the model's answer has them swapped back. The cloud sees "[PATIENT-1]",
the clinic sees "Mrs. Sunita Devi", and both texts are otherwise identical -
so the word-loss audit still holds after re-identification.

What gets caught, in order of reliability:

  * the names the app POSITIVELY knows (patient, referrer - parsed from the
    report's own fields), every occurrence
  * labelled identifiers: MRN / UHID / IP No / Aadhaar-style numbers
  * phone numbers (Indian formats), email addresses
  * calendar dates (dd/mm/yyyy and friends). The study date pins a visit;
    ages and measurements are clinical content and are left alone.

Regex, not a model - a de-identifier that itself sends the text to a model
would be a joke. This is scope-limited by design: it removes the direct
identifiers defined above, which is what leaves for the cloud; it is not a
certified DICOM PS3.15 anonymiser for releasing datasets.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

_LABELLED_ID = re.compile(
    r"\b(?:MRN|UHID|IPD?\s*No\.?|OPD\s*No\.?|Reg(?:istration)?\.?\s*No\.?|"
    r"Hosp(?:ital)?\.?\s*No\.?|Aadhaar|ABHA)\s*[:#-]?\s*([A-Za-z0-9/-]{4,20})",
    re.I,
)
_PHONE = re.compile(
    r"(?<!\d)(?:\+91[-\s]?)?[6-9]\d{4}[-\s]?\d{5}(?!\d)"
)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
_DATE = re.compile(
    r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2}|"
    r"\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"[a-z]*\.?,?\s+\d{4})\b",
    re.I,
)


@dataclass
class Deidentified:
    text: str
    mapping: dict[str, str] = field(default_factory=dict)  # placeholder -> original

    @property
    def changed(self) -> bool:
        return bool(self.mapping)


def _swap(text: str, pattern: re.Pattern, label: str, mapping: dict[str, str],
          group: int = 0) -> str:
    counter = sum(1 for k in mapping if k.startswith(f"[{label}-"))

    def replace(match: re.Match) -> str:
        nonlocal counter
        original = match.group(group)
        # The same value always gets the same placeholder, so a name used
        # twice stays consistent for the model.
        for placeholder, value in mapping.items():
            if value == original and placeholder.startswith(f"[{label}-"):
                if group == 0:
                    return placeholder
                return match.group(0).replace(original, placeholder)
        counter += 1
        placeholder = f"[{label}-{counter}]"
        mapping[placeholder] = original
        if group == 0:
            return placeholder
        return match.group(0).replace(original, placeholder)

    return pattern.sub(replace, text)


def deidentify(text: str, known_names: list[str] | None = None) -> Deidentified:
    """Direct identifiers out, placeholders in. Reversible via reidentify()."""
    mapping: dict[str, str] = {}
    out = text or ""

    # Known names first - the one identifier the app is CERTAIN about.
    counter = 0
    for name in known_names or []:
        cleaned = re.sub(r"\b(mrs?|ms|miss|dr|master|baby)\b\.?", "", name or "",
                         flags=re.I).strip()
        if len(cleaned) < 3:
            continue
        pattern = re.compile(
            r"(?:\b(?:Mrs?|Ms|Miss|Dr|Master|Baby)\.?\s+)?"
            + r"\s+".join(re.escape(part) for part in cleaned.split()),
            re.I,
        )
        if pattern.search(out):
            counter += 1
            placeholder = f"[NAME-{counter}]"
            mapping[placeholder] = pattern.search(out).group(0)
            out = pattern.sub(placeholder, out)

    out = _swap(out, _LABELLED_ID, "ID", mapping, group=1)
    out = _swap(out, _PHONE, "PHONE", mapping)
    out = _swap(out, _EMAIL, "EMAIL", mapping)
    out = _swap(out, _DATE, "DATE", mapping)
    return Deidentified(text=out, mapping=mapping)


def reidentify(text: str, mapping: dict[str, str]) -> str:
    """Placeholders back to the originals - longest placeholders first."""
    out = text or ""
    for placeholder in sorted(mapping, key=len, reverse=True):
        out = out.replace(placeholder, mapping[placeholder])
    return out


def cloud_deid_enabled() -> bool:
    """DEID_CLOUD secret/environment flag."""
    value = ""
    try:
        import streamlit as st

        if "DEID_CLOUD" in st.secrets:
            value = str(st.secrets["DEID_CLOUD"]).strip()
    except Exception:
        pass
    value = value or os.environ.get("DEID_CLOUD", "").strip()
    return value.lower() in ("1", "true", "yes", "on")


def for_cloud(text: str, known_names: list[str] | None = None) -> Deidentified:
    """
    The one call sites use: de-identify when the flag is on, pass through
    when it is off. Callers re-identify the model's answer with .mapping.
    """
    if not cloud_deid_enabled():
        return Deidentified(text=text or "", mapping={})
    return deidentify(text, known_names)
