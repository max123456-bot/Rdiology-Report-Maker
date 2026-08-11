"""
Report records: the worklist, the sign-off trail, and the patient's history.

A record is one report's life in the system:

    draft  ->  signed  ->  delivered

Each transition is stamped with who and when, both on the record and in the
tenant's audit log. The record also carries what triage decided (so the
worklist can sort stat above routine) and the measurements found in the text
(so the next study of the same patient can be compared against this one).

Comparison is deterministic and conservative, like everything else that gets
near clinical text: it matches measurements by anatomy ("left kidney",
"thyroid nodule"), reports growth or shrinkage only past a small threshold,
and says "new" or "not mentioned" rather than guessing at significance.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import storage
import triage as triage_engine

STATUSES = ("draft", "signed", "delivered")

# --------------------------------------------------------------------------- #
# Measurement extraction
# --------------------------------------------------------------------------- #

# What a measurement can attach to. Sides are captured separately so "right
# kidney" and "left kidney" are different keys.
_ANATOMY = (
    "liver", "spleen", "gallbladder", "pancreas", "kidney", "ureter", "bladder",
    "prostate", "uterus", "endometrium", "ovary", "cervix", "thyroid", "aorta",
    "portal vein", "cbd", "common bile duct", "appendix", "lymph node",
    "nodule", "cyst", "mass", "lesion", "fibroid", "myoma", "polyp", "calculus",
    "stone", "collection", "abscess", "hematoma", "haematoma", "aneurysm",
    "hernia", "effusion", "follicle", "sac", "hydrocele", "varicocele",
)
# Findings that can appear and disappear between studies. Organs cannot.
_TRANSIENT = {
    "nodule", "cyst", "mass", "lesion", "fibroid", "myoma", "polyp", "calculus",
    "stone", "collection", "abscess", "hematoma", "haematoma", "effusion",
    "hydrocele", "varicocele",
}

_ANATOMY_RE = re.compile(
    r"\b(?:(right|left|rt\.?|lt\.?)\s+)?(" + "|".join(_ANATOMY) + r")s?\b", re.I
)
_DIMS_RE = re.compile(
    r"(\d+(?:\.\d+)?)(?:\s*[x×*]\s*(\d+(?:\.\d+)?))?(?:\s*[x×*]\s*(\d+(?:\.\d+)?))?"
    r"\s*(mm|cm)\b",
    re.I,
)

_SIDE = {"rt": "right", "rt.": "right", "lt": "left", "lt.": "left"}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.;\n])\s+", text or "") if s.strip()]


def extract_measurements(text: str) -> list[dict]:
    """
    (anatomy, size) pairs from the text.

    One entry per anatomy key, first mention wins - the first statement of an
    organ's size is almost always the measurement, later mentions are prose.
    Sizes are normalised to millimetres, the largest dimension of "a x b x c".

    Anaphora carry-forward: "A cyst is seen in the left kidney. It measures
    2.5 cm." puts the size in a sentence with no anatomy at all. A sentence
    with dimensions but no anatomy inherits the PREVIOUS sentence's anatomy -
    that is how dictation continues a topic, and losing the measurement was
    worse than the mild assumption.
    """
    out: list[dict] = []
    seen: set[str] = set()
    previous_key = ""
    for sentence in _sentences(text):
        anatomy_match = _ANATOMY_RE.search(sentence)
        dims_match = _DIMS_RE.search(sentence)

        if anatomy_match:
            side = (anatomy_match.group(1) or "").lower().rstrip(".")
            side = _SIDE.get(side, side)
            organ = anatomy_match.group(2).lower()
            key = f"{side} {organ}".strip()
            via = "stated"
        elif dims_match and previous_key:
            key = previous_key       # the sentence continues the last topic
            via = "anaphora"
        else:
            continue

        if anatomy_match:
            previous_key = key
        if not dims_match or key in seen:
            continue
        values = [float(v) for v in dims_match.groups()[:3] if v]
        unit = dims_match.group(4).lower()
        size_mm = max(values) * (10 if unit == "cm" else 1)
        seen.add(key)
        out.append({
            "key": key,
            "size_mm": round(size_mm, 1),
            "stated": dims_match.group(0),
            "sentence": sentence[:160],
            "via": via,
        })
    return out


# --------------------------------------------------------------------------- #
# Patient identity
# --------------------------------------------------------------------------- #


def patient_key(name: str, age_sex: str = "") -> str:
    """
    A stable key for "the same patient", from what a report actually carries.

    Without an MRN this is name plus age/sex, normalised hard. Titles are
    stripped so "Mrs. Sunita Devi" and "SUNITA DEVI" match. Imperfect without
    a real identifier - which is why the UI always shows which prior report a
    comparison came from instead of silently trusting the match.
    """
    cleaned = re.sub(
        r"\b(mrs?|ms|miss|dr|master|baby|b/o|s/o|d/o|w/o)\b\.?", " ",
        (name or "").lower(),
    )
    cleaned = re.sub(r"[^a-z0-9]+", "-", cleaned).strip("-")
    sex = ""
    m = re.search(r"\b(male|female|m|f)\b", (age_sex or "").lower())
    if m:
        sex = m.group(1)[0]
    return f"{cleaned}-{sex}".strip("-")[:80]


_FIELD_HEADINGS = {
    "patient": ("PATIENT NAME", "PATIENT'S NAME", "NAME OF PATIENT"),
    "age_sex": ("AGE/SEX", "AGE / SEX", "AGE AND SEX", "AGE", "SEX"),
    "referrer": ("REFERRED BY", "REFERRING DOCTOR"),
    "study_date": ("DATE OF EXAMINATION", "DATE"),
}


def fields_from_blocks(blocks) -> dict:
    """Patient name, age/sex, referrer, study title - out of parsed blocks."""
    out = {"patient": "", "age_sex": "", "referrer": "", "study": "", "study_date": ""}
    pending = ""
    for b in blocks:
        if b.kind == "title" and not out["study"]:
            out["study"] = b.text.strip()
            continue
        if b.kind == "heading_inline":
            heading = b.text.rstrip(":").strip().upper()
            for field_name, headings in _FIELD_HEADINGS.items():
                if heading in headings and not out[field_name]:
                    out[field_name] = (b.trailer or "").strip()
            continue
        if b.kind == "heading":
            heading = b.text.rstrip(":").strip().upper()
            pending = next(
                (f for f, hs in _FIELD_HEADINGS.items() if heading in hs and not out[f]),
                "",
            )
            continue
        if pending and b.text.strip():
            out[pending] = b.text.strip()
            pending = ""
    return out


_MODALITIES = (
    ("usg", "USG"), ("ultrasound", "USG"), ("sonography", "USG"), ("doppler", "Doppler"),
    ("hrct", "CT"), ("ct ", "CT"), ("computed tomography", "CT"),
    ("mri", "MRI"), ("mr ", "MRI"), ("magnetic resonance", "MRI"),
    ("x-ray", "X-Ray"), ("xray", "X-Ray"), ("radiograph", "X-Ray"),
    ("mammo", "Mammography"), ("barium", "Fluoroscopy"), ("ivp", "Fluoroscopy"),
)


def modality_of(study_title: str) -> str:
    lowered = f" {(study_title or '').lower()} "
    for needle, name in _MODALITIES:
        if needle in lowered:
            return name
    return ""


# --------------------------------------------------------------------------- #
# The record
# --------------------------------------------------------------------------- #


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_record(report_text: str, blocks=None, source: str = "paste") -> dict:
    """
    A draft record for a report that just went through the formatter.

    `report_text` is the text the .docx was built from - the audited words.
    """
    fields = fields_from_blocks(blocks or [])
    triaged = (
        triage_engine.triage_blocks(blocks) if blocks
        else triage_engine.triage_text(report_text)
    )
    now = _now()
    return {
        "id": uuid.uuid4().hex[:12],
        "created": now,
        "updated": now,
        "status": "draft",
        "source": source,                      # paste | dictation | ocr | prefill
        "patient": fields["patient"],
        "age_sex": fields["age_sex"],
        "referrer": fields["referrer"],
        "study": fields["study"],
        "study_date": fields["study_date"],
        "modality": modality_of(fields["study"]),
        "patient_key": patient_key(fields["patient"], fields["age_sex"]),
        "urgency": triaged.level,
        "triage_terms": sorted({h.term for h in triaged.hits}),
        "report_text": report_text,
        "measurements": extract_measurements(report_text),
        "signed_by": "",
        "signed_at": "",
        "delivered_at": "",
        "delivered_via": "",
        "trail": [{"when": now, "what": "created", "user": ""}],
    }


def _stamp(record: dict, what: str, user: str) -> None:
    record["updated"] = _now()
    record.setdefault("trail", []).append({"when": record["updated"], "what": what,
                                           "user": user})


def sign(record: dict, user: str = "", role: str | None = None,
         critical: bool = False, justification: str = "") -> dict:
    """
    draft -> signed. Refuses to sign twice or to sign a delivered report.

    Role-gated: signing is the legally meaningful act, so only an attending
    radiologist's role may perform it (access.require raises otherwise; a
    deployment with no roles configured defaults everyone to attending, which
    is the pre-RBAC behaviour).

    Hard-stop: when the safety checks found something critical, signing
    demands a written justification - it goes on the record's trail and into
    the audit log, so overriding a safety flag is always attributable.
    """
    import access

    access.require("sign", role)
    if record.get("status") != "draft":
        raise ValueError(f"Only a draft can be signed - this report is "
                         f"{record.get('status')}.")
    if critical and not justification.strip():
        raise ValueError(
            "The safety checks flagged something critical. Signing past a "
            "critical flag needs a written justification - it is recorded, "
            "not forbidden."
        )
    record["status"] = "signed"
    record["signed_by"] = user
    record["signed_at"] = _now()
    if justification.strip():
        record["sign_justification"] = justification.strip()
        _stamp(record, f"signed past critical flag: {justification.strip()[:160]}",
               user)
        storage.log("report.sign_override", record.get("study") or record.get("id", ""),
                    detail=justification.strip()[:400], user=user)
    else:
        _stamp(record, "signed", user)

    # Non-repudiation: a keyed signature over the exact signed text. With no
    # ATTEST_KEY this is empty rather than fake - a plain hash proves
    # integrity (the attestation chain does that) but not WHO, so only a
    # secret key makes this a signature worth the name.
    import verify

    record["signature"] = verify.hmac_signature(record.get("report_text", ""))
    return record


def deliver(record: dict, user: str = "", via: str = "download",
            role: str | None = None) -> dict:
    """signed -> delivered. A draft cannot be delivered - it was never signed."""
    import access

    access.require("deliver", role)
    if record.get("status") == "draft":
        raise ValueError("Sign the report before delivering it.")
    record["status"] = "delivered"
    record["delivered_at"] = _now()
    record["delivered_via"] = via
    _stamp(record, f"delivered via {via}", user)
    return record


def save(record: dict, tenant: str | None = None) -> dict:
    """Persist and audit-log. The one write path for records."""
    tenant = tenant or storage.current_tenant()
    storage.get_store().save_report(tenant, record)
    storage.log(
        f"report.{record.get('status', 'draft')}",
        record.get("study") or record.get("id", ""),
        detail=f"{record.get('patient', '')} · {record.get('urgency', 'routine')}",
    )
    return record


def worklist(tenant: str | None = None, status: str | None = None,
             limit: int = 200) -> list[dict]:
    """Records for the worklist page, stat first, newest first within a level."""
    tenant = tenant or storage.current_tenant()
    rows = storage.get_store().list_reports(tenant, status=status, limit=limit)
    rank = {"stat": 0, "urgent": 1, "routine": 2}
    # Two stable sorts: newest first, then urgency - so inside each urgency
    # band the newest report is on top.
    rows.sort(key=lambda r: str(r.get("updated") or ""), reverse=True)
    rows.sort(key=lambda r: rank.get(str(r.get("urgency")), 2))
    return rows


def priors(record: dict, tenant: str | None = None, limit: int = 10) -> list[dict]:
    """Earlier signed/delivered reports for the same patient, newest first."""
    key = record.get("patient_key") or ""
    if not key or key in ("-", ""):
        return []
    tenant = tenant or storage.current_tenant()
    rows = storage.get_store().list_reports(tenant, patient_key=key, limit=limit * 3)
    return [
        r for r in rows
        if r.get("id") != record.get("id")
        and r.get("status") in ("signed", "delivered")
        # The same text is the same report, not a prior - comparing a report
        # against its own just-saved copy would always say "stable".
        and (r.get("report_text") or "").strip() != (record.get("report_text") or "").strip()
    ][:limit]


# --------------------------------------------------------------------------- #
# Prior comparison
# --------------------------------------------------------------------------- #


@dataclass
class Delta:
    kind: str      # grew | shrank | stable | new | gone
    key: str       # "left kidney", "nodule", ...
    before: str    # as stated in the prior
    after: str     # as stated now
    note: str


def _months_between(a: str, b: str) -> str:
    try:
        first = datetime.fromisoformat(a.replace("Z", "+00:00"))
        second = datetime.fromisoformat(b.replace("Z", "+00:00"))
        days = abs((second - first).days)
    except Exception:
        return ""
    if days < 45:
        return f"{days} days"
    return f"{round(days / 30.4)} months"


def compare(current: dict, prior: dict) -> list[Delta]:
    """
    Measurement-by-measurement diff against one prior report.

    Growth under 2 mm and under 10% is reported as stable - ultrasound
    calliper placement alone varies that much, and a false "grew" is worse
    than a quiet "stable".
    """
    interval = _months_between(prior.get("created", ""), current.get("created", ""))
    now = {m["key"]: m for m in current.get("measurements", [])}
    then = {m["key"]: m for m in prior.get("measurements", [])}
    deltas: list[Delta] = []

    for key in sorted(set(now) | set(then)):
        organ = key.split()[-1]
        if key in now and key in then:
            a, b = then[key]["size_mm"], now[key]["size_mm"]
            change = b - a
            threshold = max(2.0, a * 0.10)
            if abs(change) < threshold:
                kind, note = "stable", "No significant interval change."
            elif change > 0:
                kind = "grew"
                note = f"Increased by {round(change, 1)} mm"
                note += f" over {interval}." if interval else "."
            else:
                kind = "shrank"
                note = f"Decreased by {round(-change, 1)} mm"
                note += f" over {interval}." if interval else "."
            deltas.append(Delta(kind=kind, key=key, before=then[key]["stated"],
                                after=now[key]["stated"], note=note))
        elif key in now:
            if organ in _TRANSIENT:
                deltas.append(Delta(
                    kind="new", key=key, before="", after=now[key]["stated"],
                    note="Not mentioned in the prior report - possibly new.",
                ))
        else:
            if organ in _TRANSIENT:
                deltas.append(Delta(
                    kind="gone", key=key, before=then[key]["stated"], after="",
                    note="Measured in the prior report, not mentioned now. "
                         "Resolved, or missed?",
                ))
    return deltas


def comparison_summary(deltas: list[Delta]) -> str:
    if not deltas:
        return "Nothing comparable between the two reports."
    counts: dict[str, int] = {}
    for d in deltas:
        counts[d.kind] = counts.get(d.kind, 0) + 1
    order = ("grew", "new", "shrank", "gone", "stable")
    return " · ".join(f"{counts[k]} {k}" for k in order if k in counts)
