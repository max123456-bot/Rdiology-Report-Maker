"""
The processing engine as a service: FastAPI over every offline module.

The Streamlit app stays exactly as it is - this file does not change it.
What this adds is a second front door for systems, not people: a RIS, an
integration engine, or a batch script can call the same parsing, validation,
triage, guideline and export logic over HTTP without a browser in sight.

Run it separately from the UI:

    uvicorn api:app --host 0.0.0.0 --port 8000

Every endpoint here is offline and deterministic - no Gemini key needed, no
network called. The AI paths are deliberately NOT exposed: an unattended
HTTP caller cannot review a draft, and this project does not produce
clinical text nobody reviewed.

Docs are self-serving: /docs (Swagger) and /redoc once running.
"""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import guidelines as guidelines_engine
import impression as impression_engine
import interop
import records as records_engine
import triage as triage_engine
import validate as validate_engine
import verify as verify_engine
from hc_format import parse_report
from schemas import RadiologyReportSchema, report_from_blocks

app = FastAPI(
    title="HC FORMAT report engine",
    version="1.0",
    description="Deterministic radiology report processing: parse, validate, "
                "triage, guideline advice, impression proposals, HL7/FHIR export.",
)


class ReportIn(BaseModel):
    text: str = Field(min_length=1, description="The raw report text.")


class TwoTextsIn(BaseModel):
    source: str = Field(min_length=1)
    output: str = Field(min_length=1)


class FindingOut(BaseModel):
    severity: str
    title: str
    detail: str = ""
    where: str = ""


class HL7In(BaseModel):
    text: str = Field(min_length=1)
    patient: str = ""
    age_sex: str = ""
    study: str = ""
    referrer: str = ""
    status: str = "signed"
    facility: str = ""


def _blocks(text: str):
    parsed = parse_report(text)
    if not parsed.blocks:
        raise HTTPException(status_code=422, detail="No parseable report structure.")
    return parsed.blocks


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "engine": "hc-format", "version": app.version}


@app.post("/parse", response_model=RadiologyReportSchema)
async def parse(body: ReportIn) -> RadiologyReportSchema:
    """Raw text to the structured report schema."""
    return report_from_blocks(_blocks(body.text))


@app.post("/validate", response_model=list[FindingOut])
async def validate(body: ReportIn) -> list[FindingOut]:
    """Every clinical safety check, sorted most severe first."""
    result = validate_engine.validate(_blocks(body.text))
    return [FindingOut(severity=f.severity, title=f.title, detail=f.detail,
                       where=f.where) for f in result.sorted()]


@app.post("/triage")
async def triage(body: ReportIn) -> dict:
    """stat | urgent | routine, with the sentences that decided it."""
    result = triage_engine.triage_blocks(_blocks(body.text))
    return {
        "level": result.level,
        "hits": [{"term": h.term, "level": h.level, "sentence": h.sentence}
                 for h in result.hits],
    }


@app.post("/guidelines")
async def guideline_advice(body: ReportIn) -> list[dict]:
    """Consensus-guideline suggestions (Fleischner, BI-RADS, ...)."""
    return [
        {"system": a.system, "kind": a.kind, "trigger": a.trigger,
         "recommendation": a.recommendation, "detail": a.detail}
        for a in guidelines_engine.advise_blocks(_blocks(body.text))
    ]


@app.post("/impression")
async def impression(body: ReportIn) -> dict:
    """
    Deterministic impression proposals from a report's findings - verbatim
    sentences only, never invented text. Sub-second by construction.
    """
    blocks = _blocks(body.text)
    findings_text = "\n".join(
        b.text for b in blocks
        if (b.section or "").upper() in ("FINDINGS", "OBSERVATIONS")
        and b.text.strip()
    )
    proposals = impression_engine.propose_from_findings(findings_text)
    normal = impression_engine.normal_study_line(findings_text)
    return {
        "proposals": proposals,
        "normal_study_line": normal,
        "as_block": impression_engine.as_impression_block(proposals),
    }


@app.post("/audit")
async def audit_alignment(body: TwoTextsIn) -> dict:
    """
    Word-level reconciliation between a source and any derived text: every
    dropped span with its position and context.
    """
    plan = verify_engine.reconciliation_plan(body.source, body.output)
    return {
        "identical": not plan,
        "dropped_spans": [
            {"text": p.text, "word_index": p.word_index,
             "before": p.before, "after": p.after}
            for p in plan
        ],
    }


@app.post("/hl7")
async def hl7(body: HL7In) -> dict:
    """An HL7 v2.5 ORU^R01 message for the given report."""
    record = _record_from(body)
    return {"hl7": interop.hl7_oru(record, facility=body.facility)}


@app.post("/fhir")
async def fhir(body: HL7In) -> dict:
    """A FHIR R4 Bundle (Patient + DiagnosticReport) for the given report."""
    record = _record_from(body)
    return interop.fhir_diagnostic_report(record)


def _record_from(body: HL7In) -> dict:
    blocks = _blocks(body.text)
    record = records_engine.new_record(body.text, blocks)
    for field_name in ("patient", "age_sex", "study", "referrer"):
        value = getattr(body, field_name)
        if value:
            record[field_name] = value
    if body.status in ("draft", "signed", "delivered"):
        record["status"] = body.status
    return record


@app.post("/anatomy")
async def anatomy_tree(body: ReportIn) -> dict:
    """
    The findings as a REGION → ORGAN → SUBPART tree with deterministic
    coreference. A structured VIEW of the text - the text itself is untouched.
    """
    import anatomy

    blocks = _blocks(body.text)
    findings_text = "\n".join(
        b.text for b in blocks
        if (b.section or "").upper() in ("FINDINGS", "OBSERVATIONS")
        and b.text.strip()
    )
    tree = anatomy.findings_tree(findings_text)
    return {
        "tree": tree,
        "flat": [{"path": path, "sentence": sentence}
                 for path, sentence in anatomy.flatten(tree)],
    }


class DictationIn(BaseModel):
    text: str = Field(min_length=1)
    vocabulary: list[str] = Field(default_factory=list)


@app.post("/dictation-cleanup")
async def dictation_cleanup(body: DictationIn) -> dict:
    """
    Deterministic post-ASR cleanup: spoken numbers to figures, units
    normalised, medical notation joined. Suggestions are advisory only -
    the same never-auto-apply rule as the UI.
    """
    import dictation_fix

    result = dictation_fix.clean(body.text, body.vocabulary)
    return {
        "text": result.text,
        "changed": result.changed,
        "note": result.note,
        "suggestions": [
            {"heard": s.heard, "suggested": s.suggested,
             "confidence": s.confidence, "reason": s.reason}
            for s in (result.suggestions or [])
        ],
    }


class MacroIn(BaseModel):
    text: str
    macros: Optional[dict[str, str]] = None


@app.post("/expand-macros")
async def expand_macros(body: MacroIn) -> dict:
    """Expand macro triggers (built-ins plus any supplied) in the text."""
    import templates

    template = None
    if body.macros:
        template = templates.copy_of(templates.HC_FORMAT, "api")
        template.macros = dict(body.macros)
    expanded, used = templates.expand_macros(body.text, template)
    return {"text": expanded, "used": used}
