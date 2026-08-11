"""
Structured report schemas - the contract every AI output must satisfy.

Free text in, validated structure out. Anything an AI path returns that
claims to be a report goes through these Pydantic models before the app
touches it: a missing field, a wrong type, or an empty impression fails
loudly at the boundary instead of quietly downstream.

Also the shape of the FastAPI layer's responses (api.py), so an integrating
RIS gets one stable, versioned schema.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = "1.0"


class ImpressionItem(BaseModel):
    finding_id: int = Field(ge=1)
    impression_text: str = Field(min_length=1)
    is_critical: bool = False
    recommendation: Optional[str] = None

    @field_validator("impression_text")
    @classmethod
    def _not_placeholder(cls, value: str) -> str:
        lowered = value.strip().lower()
        if not lowered or lowered in ("tbd", "n/a", "...", "xxx"):
            raise ValueError("impression_text is a placeholder, not a finding")
        return value.strip()


class RadiologyReportSchema(BaseModel):
    schema_version: str = SCHEMA_VERSION
    study: str = ""
    clinical_history: str = ""
    technique: str = ""
    findings: Dict[str, str] = Field(default_factory=dict)  # organ/system -> text
    impression: List[ImpressionItem] = Field(default_factory=list)

    @field_validator("findings")
    @classmethod
    def _findings_not_empty_strings(cls, value: Dict[str, str]) -> Dict[str, str]:
        return {k: v for k, v in value.items() if str(v).strip()}


def report_from_blocks(blocks) -> RadiologyReportSchema:
    """
    A structured view of a parsed report - deterministic, from hc_format
    Blocks. Organ subheadings become findings keys; impression bullets become
    ImpressionItems, with is_critical set by the triage engine.
    """
    import triage

    study = ""
    history: list[str] = []
    technique: list[str] = []
    findings: dict[str, list[str]] = {}
    impression_lines: list[str] = []

    section = ""
    organ = "General"
    for b in blocks:
        if b.kind == "title":
            study = b.text
            continue
        if b.kind in ("heading", "heading_inline"):
            section = b.text.rstrip(":").strip().upper()
            organ = "General"
            if b.kind == "heading_inline" and b.trailer:
                if section in ("CLINICAL HISTORY", "CLINICAL INDICATION", "HISTORY",
                               "INDICATION", "CLINICAL DETAILS"):
                    history.append(b.trailer)
                elif section in ("TECHNIQUE", "PROTOCOL"):
                    technique.append(b.trailer)
            continue
        if b.kind == "subheading":
            organ = b.text.rstrip(":").strip() or "General"
            if b.trailer:
                findings.setdefault(organ, []).append(b.trailer)
            continue
        text = b.text.strip()
        if not text:
            continue
        if section in ("CLINICAL HISTORY", "CLINICAL INDICATION", "HISTORY",
                       "INDICATION", "CLINICAL DETAILS"):
            history.append(text)
        elif section in ("TECHNIQUE", "PROTOCOL"):
            technique.append(text)
        elif section in ("FINDINGS", "OBSERVATIONS"):
            findings.setdefault(organ, []).append(text)
        elif section in ("IMPRESSION", "CONCLUSION", "OPINION"):
            impression_lines.append(text)

    items: list[ImpressionItem] = []
    for i, line in enumerate(impression_lines, start=1):
        triaged = triage.triage_text(line)
        items.append(ImpressionItem(
            finding_id=i,
            impression_text=line,
            is_critical=triaged.level == "stat",
        ))

    return RadiologyReportSchema(
        study=study,
        clinical_history=" ".join(history),
        technique=" ".join(technique),
        findings={k: " ".join(v) for k, v in findings.items()},
        impression=items,
    )


def impression_items_from_points(points: list[str],
                                 recommendations: dict[int, str] | None = None
                                 ) -> List[ImpressionItem]:
    """Validated ImpressionItems from plain bullet strings (the AI's output)."""
    import triage

    items: list[ImpressionItem] = []
    for i, point in enumerate(points, start=1):
        text = str(point).strip()
        if not text:
            continue
        items.append(ImpressionItem(
            finding_id=i,
            impression_text=text,
            is_critical=triage.triage_text(text).level == "stat",
            recommendation=(recommendations or {}).get(i),
        ))
    if not items:
        raise ValueError("No usable impression points.")
    return items
