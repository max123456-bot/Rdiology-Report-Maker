"""
Bulk processing: many reports in, one audited ZIP out.

The Streamlit tab stays thin; everything with logic in it lives here, where
the checks can reach it:

  split_pasted()    many reports in one paste, separated by `---`
  from_csv()        a diagnostic centre's spreadsheet - one report per row,
                    metadata columns becoming the report's own headings
  route_template()  50 reports, several doctors: each report goes to the
                    template of the doctor whose name is actually in it
  process_one()     parse -> build -> word-loss audit -> QC -> triage,
                    per report, exceptions contained per row
  manifest_csv()    the medico-legal manifest: SHA-256 of source and output,
                    audit verdict, QC count, timestamp - one row per report
  zip_bytes()       the deliverable: every .docx plus the manifest

No Celery, no Redis, deliberately: one process serves this app, and a
thread-out queue would add two services to operate for the same wall-clock
time. The UI shows per-report progress from a plain loop; a real queue earns
its keep only when processing moves off this machine.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class BatchItem:
    label: str
    text: str
    patient: str = ""
    age_sex: str = ""
    study: str = ""
    patient_id: str = ""


@dataclass
class BatchResult:
    label: str
    text: str = ""            # the exact source, for worklist hand-off
    title: str = ""
    filename: str = ""
    docx: bytes = b""
    audit_ok: bool = False
    audit_summary: str = ""
    source_tokens: int = 0
    qc_critical: list[str] = field(default_factory=list)
    qc_warnings: int = 0
    urgency: str = "routine"
    patient: str = ""
    modality: str = ""
    template_name: str = ""
    routed: str = ""          # how the template was chosen
    source_sha256: str = ""
    docx_sha256: str = ""
    error: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.docx) and self.audit_ok and not self.qc_critical \
            and not self.error


# --------------------------------------------------------------------------- #
# Intake
# --------------------------------------------------------------------------- #


def split_pasted(text: str) -> list[str]:
    """Many reports in one paste, separated by a line of ---."""
    return [c.strip() for c in re.split(r"^\s*-{3,}\s*$", text or "", flags=re.M)
            if c.strip()]


# Spreadsheet column names, as centres actually write them. Case-insensitive,
# spaces/underscores interchangeable.
_COLUMN_ALIASES = {
    "patient": ("patient_name", "patient", "name", "pt_name"),
    "patient_id": ("patient_id", "mrn", "uhid", "reg_no", "id"),
    "age_sex": ("age_sex", "age/sex", "agesex", "age", "age_and_sex"),
    "study": ("study_name", "study", "examination", "modality_study", "exam"),
    "text": ("raw_dictation_text", "dictation", "report_text", "text",
             "findings", "report", "raw_text"),
}


def _canon(name: str) -> str:
    return re.sub(r"[^a-z0-9/]", "_", (name or "").strip().lower()).strip("_")


def _map_columns(header: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for index, raw in enumerate(header):
        name = _canon(raw)
        for field_name, aliases in _COLUMN_ALIASES.items():
            if field_name not in out and name in aliases:
                out[field_name] = index
    return out


def _item_from_row(row: list[str], columns: dict[str, int], number: int) -> BatchItem | None:
    def cell(field_name: str) -> str:
        index = columns.get(field_name)
        return str(row[index]).strip() if index is not None and index < len(row) \
            and row[index] is not None else ""

    dictation = cell("text")
    if not dictation:
        return None
    item = BatchItem(
        label=f"row_{number}",
        text="",
        patient=cell("patient"),
        age_sex=cell("age_sex"),
        study=cell("study"),
        patient_id=cell("patient_id"),
    )
    # The metadata becomes the report's own headings, so the formatter, the
    # identifier checks and the worklist all see it exactly as if typed.
    lines: list[str] = []
    if item.study:
        lines.append(item.study.upper()
                     + ("" if item.study.upper().endswith("REPORT") else " REPORT"))
        lines.append("")
    if item.patient:
        lines.append(f"PATIENT NAME: {item.patient}")
    if item.age_sex:
        lines.append(f"AGE/SEX: {item.age_sex}")
    if item.patient:
        item.label = re.sub(r"[^A-Za-z0-9]+", "_", item.patient).strip("_") \
            or item.label
    if lines:
        lines.append("")
    lines.append(dictation)
    item.text = "\n".join(lines)
    return item


def from_csv(data: bytes) -> list[BatchItem]:
    """One report per spreadsheet row. Rows with no dictation are skipped."""
    text = data.decode("utf-8-sig", "replace")
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if any(str(c).strip() for c in row)]
    if not rows:
        return []
    columns = _map_columns([str(c) for c in rows[0]])
    if "text" not in columns:
        raise ValueError(
            "No dictation column found. The sheet needs one of: "
            + ", ".join(_COLUMN_ALIASES["text"])
            + " (plus optional Patient_Name, Age_Sex, Study_Name, Patient_ID)."
        )
    items = []
    for number, row in enumerate(rows[1:], start=1):
        item = _item_from_row([str(c) for c in row], columns, number)
        if item:
            items.append(item)
    return items


def from_xlsx(data: bytes) -> list[BatchItem]:
    """The same, from Excel. Needs openpyxl; says so when absent."""
    try:
        import openpyxl
    except ImportError as exc:
        raise ValueError(
            "Reading .xlsx needs openpyxl (pip install openpyxl) - or save the "
            "sheet as CSV, which needs nothing."
        ) from exc
    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True,
                                      data_only=True)
    sheet = workbook.active
    rows = [[("" if c is None else str(c)) for c in row]
            for row in sheet.iter_rows(values_only=True)]
    rows = [row for row in rows if any(c.strip() for c in row)]
    if not rows:
        return []
    columns = _map_columns(rows[0])
    if "text" not in columns:
        raise ValueError(
            "No dictation column found in the sheet - see the CSV column names "
            "in the caption above."
        )
    items = []
    for number, row in enumerate(rows[1:], start=1):
        item = _item_from_row(row, columns, number)
        if item:
            items.append(item)
    return items


# --------------------------------------------------------------------------- #
# Doctor routing
# --------------------------------------------------------------------------- #


def route_template(text: str, all_templates: dict, default):
    """
    (template, how) for one report: the template whose doctor's name appears
    in the text - checked in the signature zone (the last lines) first, where
    a name is a signature rather than a mention. No match, or an ambiguous
    one, falls back to the selected template and says so.
    """
    candidates = []
    for tpl in all_templates.values():
        doctor = (getattr(tpl, "doctor", "") or "").strip()
        if not doctor or getattr(tpl, "builtin", False):
            continue
        cleaned = re.sub(r"^\s*(?:dr|prof)\.?\s+", "", doctor, flags=re.I).strip()
        if len(cleaned) < 3:
            continue
        pattern = re.compile(
            r"(?:\b(?:dr|prof)\.?\s+)?" +
            r"\s+".join(re.escape(p) for p in cleaned.split()),
            re.I,
        )
        tail = text[-400:]
        if pattern.search(tail):
            candidates.append((tpl, "signature"))
        elif pattern.search(text):
            candidates.append((tpl, "mention"))

    signatures = [c for c in candidates if c[1] == "signature"]
    pool = signatures or candidates
    if len(pool) == 1:
        tpl, how = pool[0]
        return tpl, f"routed to {tpl.doctor or tpl.name} ({how})"
    if len(pool) > 1:
        return default, ("ambiguous - " +
                         " and ".join(t.doctor or t.name for t, _ in pool)
                         + " both matched; using the selected template")
    return default, "selected template (no doctor name found)"


# --------------------------------------------------------------------------- #
# Processing
# --------------------------------------------------------------------------- #


def process_one(label: str, text: str, template, *, letterhead: dict | None,
                page_numbers: bool, letterhead_text: str, opts,
                blocks_builder=None, routed: str = "") -> BatchResult:
    """
    One report, end to end. Never raises - a poisoned row must not kill the
    other 99, so every exception lands in .error for the QC table.
    """
    import records as records_engine
    import triage as triage_engine
    import validate as validate_engine
    from hc_format import build_docx, parse_report
    from verify import audit as run_audit

    result = BatchResult(label=label, text=text or "",
                         template_name=template.name, routed=routed)
    result.source_sha256 = hashlib.sha256((text or "").encode("utf-8")).hexdigest()
    try:
        if not (text or "").strip():
            raise ValueError("The report is empty.")
        if blocks_builder is not None:
            blocks = blocks_builder(text)
        else:
            blocks = parse_report(text, opts).blocks
        if not blocks:
            raise ValueError("Nothing parseable came out of the text.")

        result.title = next((b.text for b in blocks if b.kind == "title"), label)
        fields = records_engine.fields_from_blocks(blocks)
        result.patient = fields["patient"]
        result.modality = records_engine.modality_of(fields["study"] or result.title)

        result.docx = build_docx(blocks, template=template,
                                 letterhead=letterhead, page_numbers=page_numbers)
        result.docx_sha256 = hashlib.sha256(result.docx).hexdigest()

        check = run_audit(text, result.docx, letterhead_text=letterhead_text,
                          page_numbers=page_numbers,
                          preserve_as_is=getattr(opts, "preserve_as_is", False))
        result.audit_ok = check.ok
        result.audit_summary = check.summary
        result.source_tokens = check.source_tokens

        qc = validate_engine.validate(blocks)
        result.qc_critical = [f.title for f in qc.critical]
        result.qc_warnings = sum(1 for f in qc.findings if f.severity == "warning")
        result.urgency = triage_engine.triage_blocks(blocks).level
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    return result


# --------------------------------------------------------------------------- #
# The deliverable
# --------------------------------------------------------------------------- #


def manifest_csv(results: list[BatchResult]) -> str:
    """
    batch_audit_manifest.csv - the medico-legal record that travels INSIDE
    the ZIP: for every report, the SHA-256 of the exact source text and the
    exact .docx bytes, the word-loss verdict, the QC count and the UTC
    timestamp. Recompute either hash later and any tampering shows.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow([
        "filename", "source_label", "patient", "modality", "template",
        "word_loss_audit", "source_tokens", "qc_critical_count",
        "qc_critical_titles", "urgency", "sha256_source_text",
        "sha256_docx", "error", "generated_utc",
    ])
    for r in results:
        writer.writerow([
            r.filename, r.label, r.patient, r.modality, r.template_name,
            "PASS" if r.audit_ok else ("ERROR" if r.error else "FAIL"),
            r.source_tokens, len(r.qc_critical), "; ".join(r.qc_critical),
            r.urgency, r.source_sha256, r.docx_sha256, r.error, now,
        ])
    return out.getvalue()


def safe_filename(title: str) -> str:
    """A filesystem-safe .docx name from a report title."""
    stem = re.sub(r"[^A-Za-z0-9 _-]", "", title or "").strip().replace(" ", "_")
    return (stem[:80] or "report") + ".docx"


def zip_bytes(results: list[BatchResult]) -> bytes:
    """Every generated .docx plus the manifest, filenames de-duplicated."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, r in enumerate(results, start=1):
            if not r.docx:
                continue
            name = safe_filename(r.title or r.label)
            if name in zf.namelist():
                name = name.replace(".docx", f"_{i}.docx")
            r.filename = name
            zf.writestr(name, r.docx)
        zf.writestr("batch_audit_manifest.csv", manifest_csv(results))
    return buf.getvalue()
