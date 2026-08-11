"""
Offline checks for the clinical intelligence layer:

    triage.py      - stat/urgent/routine, negation, history exclusion
    guidelines.py  - Fleischner sizing, scoring-system triggers
    impression.py  - auto-impression proposals never invent text
    records.py     - lifecycle transitions, measurement extraction, comparison
    storage.py     - report records on files and SQLite

    python clinical_check.py

No network, no key, no database server needed.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import guidelines
import impression
import records
import storage
import triage
from hc_format import parse_report

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("  ok    " if condition else "  FAIL  ") + message)
    if not condition:
        failures.append(message)


# --------------------------------------------------------------------------- #
print("\ntriage")
# --------------------------------------------------------------------------- #

result = triage.triage_text(
    "Large right subdural hematoma with 8 mm midline shift. "
    "Rest of the brain parenchyma is normal."
)
check(result.level == "stat", f"subdural + midline shift is stat ({result.level})")
check(len(result.hits) == 2, f"both stat findings hit ({[h.term for h in result.hits]})")

result = triage.triage_text("No evidence of pulmonary embolism. Normal study.")
check(result.level == "routine", f"negated PE does not trigger ({result.level})")

result = triage.triage_text("Old infarct in the left MCA territory.")
check(result.level == "routine", f"old infarct is not acute ({result.level})")

result = triage.triage_text(
    "Findings are consistent with acute appendicitis with a small local collection."
)
check(result.level == "urgent", f"appendicitis is urgent ({result.level})")

# "rule out PE" in the history must not trigger when findings are clean.
history_report = parse_report(
    "CT PULMONARY ANGIOGRAM REPORT\n\n"
    "CLINICAL HISTORY: Chest pain, rule out pulmonary embolism.\n\n"
    "FINDINGS:\nThe pulmonary arteries opacify normally. No filling defect.\n\n"
    "IMPRESSION:\n- Normal CT pulmonary angiogram."
)
result = triage.triage_blocks(history_report.blocks)
check(result.level == "routine",
      f"'rule out PE' in the history does not trigger ({result.level})")

positive_report = parse_report(
    "CT PULMONARY ANGIOGRAM REPORT\n\n"
    "FINDINGS:\nA filling defect is seen - saddle embolus straddling the bifurcation.\n\n"
    "IMPRESSION:\n- Saddle embolus."
)
result = triage.triage_blocks(positive_report.blocks)
check(result.level == "stat", f"a saddle embolus in findings is stat ({result.level})")

check(triage.worse("urgent", "stat") == "stat", "worse() picks stat over urgent")
check(triage.worse("routine", "urgent") == "urgent", "worse() picks urgent over routine")


# --------------------------------------------------------------------------- #
print("\nguidelines")
# --------------------------------------------------------------------------- #

advice = guidelines.advise_text(
    "A 7 mm solid nodule is seen in the right upper lobe.", title="CT CHEST"
)
check(len(advice) == 1 and advice[0].system == "Fleischner 2017",
      f"a lung nodule fires Fleischner ({[a.system for a in advice]})")
check("6-8 mm" in advice[0].recommendation,
      "a 7 mm nodule gets the 6-8 mm recommendation")

advice = guidelines.advise_text(
    "A 4 mm ground-glass nodule in the left lower lobe.", title="HRCT CHEST"
)
check(advice and "no routine follow-up" in advice[0].recommendation.lower(),
      "a small ground-glass nodule needs no routine follow-up")

advice = guidelines.advise_text(
    "A 12 mm solid nodule in the left upper lobe.", title="CT CHEST"
)
check(advice and "8 mm" in advice[0].recommendation,
      "a large nodule gets the over-8mm pathway")

advice = guidelines.advise_text(
    "A 7 mm nodule in the right upper lobe. Follow-up per Fleischner guidelines "
    "is advised.", title="CT CHEST",
)
check(not any(a.system == "Fleischner 2017" for a in advice),
      "no Fleischner advice when the report already cites Fleischner")

advice = guidelines.advise_text("No pulmonary nodule is seen.", title="CT CHEST")
check(not advice, "a negated nodule fires nothing")

advice = guidelines.advise_text(
    "A hypoechoic nodule in the right thyroid lobe measuring 11 mm."
)
systems = [a.system for a in advice]
check("ACR TI-RADS" in systems, f"a thyroid nodule asks for TI-RADS ({systems})")
check("Fleischner 2017" not in systems, "a thyroid nodule does not fire Fleischner")

advice = guidelines.advise_text(
    "An irregular mass in the upper outer quadrant of the left breast."
)
check(any(a.system == "ACR BI-RADS" for a in advice), "a breast mass asks for BI-RADS")

advice = guidelines.advise_text(
    "An irregular mass in the left breast. BI-RADS 4 - biopsy recommended."
)
check(not any(a.system == "ACR BI-RADS" for a in advice),
      "no BI-RADS advice when a category is already given")

advice = guidelines.advise_text("A complex cyst in the right kidney.")
check(any(a.system == "Bosniak" for a in advice), "a renal cyst asks for Bosniak")


# --------------------------------------------------------------------------- #
print("\nimpression")
# --------------------------------------------------------------------------- #

findings = (
    "Liver is enlarged, measures 16.8 cm, with diffusely increased echogenicity "
    "suggestive of fatty infiltration.\n"
    "Gallbladder is normal. No calculus.\n"
    "Both kidneys are normal in size and echotexture.\n"
    "A 6.2 mm calculus is seen in the lower ureter with mild hydronephrosis."
)
proposals = impression.propose_from_findings(findings)
check(len(proposals) == 2, f"two abnormal findings proposed ({len(proposals)})")
check(any("16.8 cm" in p for p in proposals), "the liver line survives verbatim")
check(any("6.2 mm" in p for p in proposals), "the calculus line survives verbatim")
check(not any("Gallbladder" in p for p in proposals), "normal lines stay out")
for p in proposals:
    check(p in findings, f"proposal is verbatim from the findings: “{p[:40]}...”")

normal = impression.normal_study_line("Liver, gallbladder and both kidneys are normal.")
check(normal is not None and "No significant abnormality" in normal,
      "an all-normal study proposes the normal line")
check(impression.normal_study_line(findings) is None,
      "an abnormal study does not propose the normal line")

block = impression.as_impression_block(["Fatty liver", "Ureteric calculus"])
check(block == "- Fatty liver.\n- Ureteric calculus.", "bullets render correctly")

import ai_parser  # noqa: E402  (prompt builders are pure)

prompt = ai_parser.build_impression_prompt(findings)
check("--- FINDINGS ---" in prompt and "16.8 cm" in prompt,
      "the impression prompt carries the findings")
check("Never add a finding" in prompt, "the prompt forbids invention")

prefill_prompt = ai_parser.build_prefill_prompt("Chest X-ray PA view")
check("Chest X-ray PA view" in prefill_prompt, "prefill prompt carries the context")
check("requires radiologist review" in prefill_prompt,
      "prefill prompt demands the review disclaimer")


# --------------------------------------------------------------------------- #
print("\nrecords - extraction and identity")
# --------------------------------------------------------------------------- #

measurements = records.extract_measurements(
    "Liver measures 16.2 cm and is normal in echotexture. "
    "A mass of 4.2 x 3.1 cm is seen in the right kidney. "
    "The left kidney measures 9.8 cm."
)
by_key = {m["key"]: m for m in measurements}
check("liver" in by_key and by_key["liver"]["size_mm"] == 162.0,
      f"liver 16.2 cm -> 162 mm ({by_key.get('liver')})")
check("left kidney" in by_key and by_key["left kidney"]["size_mm"] == 98.0,
      "left kidney keyed with its side")
check(any(k.endswith("mass") for k in by_key)
      and max(m["size_mm"] for m in measurements) == 162.0,
      "multi-dimension mass captured")
mass_key = next(k for k in by_key if k.endswith("mass"))
check(by_key[mass_key]["size_mm"] == 42.0, "4.2 x 3.1 cm mass -> largest dim 42 mm")

check(records.patient_key("Mrs. Sunita Devi", "45Y/F")
      == records.patient_key("SUNITA DEVI", "45 years female"),
      "the same patient matches across title and case differences")
check(records.patient_key("Sunita Devi", "45/F")
      != records.patient_key("Sunita Devi", "45/M"),
      "a different sex is a different patient key")

sample = parse_report(
    "USG ABDOMEN REPORT\n\n"
    "PATIENT NAME: Mrs. Sunita Devi\n"
    "AGE/SEX: 45 Years / Female\n"
    "REFERRED BY: Dr. Mehta\n\n"
    "FINDINGS:\nLiver measures 16.2 cm, echotexture is coarse.\n\n"
    "IMPRESSION:\n- Hepatomegaly."
)
fields = records.fields_from_blocks(sample.blocks)
check(fields["patient"] == "Mrs. Sunita Devi", f"patient name extracted ({fields['patient']!r})")
check("45" in fields["age_sex"], "age/sex extracted")
check(fields["referrer"] == "Dr. Mehta", "referrer extracted")
check(fields["study"] == "USG ABDOMEN REPORT", "study title extracted")
check(records.modality_of(fields["study"]) == "USG", "modality guessed from the title")


# --------------------------------------------------------------------------- #
print("\nrecords - lifecycle")
# --------------------------------------------------------------------------- #

raw = ("USG ABDOMEN REPORT\n\nPATIENT NAME: Mrs. Sunita Devi\nAGE/SEX: 45Y/F\n\n"
       "FINDINGS:\nLiver measures 16.2 cm.\n\nIMPRESSION:\n- Hepatomegaly.")
rec = records.new_record(raw, sample.blocks)
check(rec["status"] == "draft", "a new record is a draft")
check(rec["patient_key"], "a new record carries a patient key")
check(rec["urgency"] == "routine", "hepatomegaly is routine")

try:
    records.deliver(rec)
    check(False, "delivering a draft was allowed")
except ValueError:
    check(True, "a draft cannot be delivered before signing")

records.sign(rec, user="dr-a")
check(rec["status"] == "signed" and rec["signed_by"] == "dr-a", "signing stamps the user")

try:
    records.sign(rec)
    check(False, "double-signing was allowed")
except ValueError:
    check(True, "a report cannot be signed twice")

records.deliver(rec, user="dr-a", via="download")
check(rec["status"] == "delivered", "signed -> delivered works")
check([t["what"] for t in rec["trail"]][:1] == ["created"], "the trail starts at created")
check(len(rec["trail"]) == 3, f"every transition is on the trail ({len(rec['trail'])})")

stat_rec = records.new_record(
    "CT BRAIN REPORT\n\nFINDINGS:\nAcute subdural hematoma with midline shift.\n\n"
    "IMPRESSION:\n- Acute subdural hematoma."
)
check(stat_rec["urgency"] == "stat", "a subdural drives record urgency to stat")
check("midline shift" in stat_rec["triage_terms"], "triage terms are stored")


# --------------------------------------------------------------------------- #
print("\nrecords - comparison")
# --------------------------------------------------------------------------- #

prior = records.new_record(
    "USG THYROID\n\nFINDINGS:\nA nodule in the right thyroid lobe measures 8 mm. "
    "A cyst in the left lobe measures 5 mm.\n\nIMPRESSION:\n- Thyroid nodule."
)
prior["created"] = "2026-01-01T00:00:00+00:00"
current = records.new_record(
    "USG THYROID\n\nFINDINGS:\nA nodule in the right thyroid lobe measures 14 mm.\n\n"
    "IMPRESSION:\n- Thyroid nodule, larger."
)
current["created"] = "2026-08-01T00:00:00+00:00"
deltas = records.compare(current, prior)
kinds = {d.key: d.kind for d in deltas}
check(kinds.get("right nodule") == "grew" or kinds.get("nodule") == "grew",
      f"8 -> 14 mm reads as growth ({kinds})")
grew = next(d for d in deltas if d.kind == "grew")
check("7 months" in grew.note or "6 months" in grew.note,
      f"growth note carries the interval ({grew.note})")
check(any(d.kind == "gone" for d in deltas), "the vanished cyst is flagged")

stable_deltas = records.compare(
    current,
    {**prior, "measurements": records.extract_measurements(
        "A nodule in the right thyroid lobe measures 13.5 mm.")},
)
check(all(d.kind == "stable" for d in stable_deltas if d.key.endswith("nodule")),
      "a 0.5 mm difference reads as stable, not growth")


# --------------------------------------------------------------------------- #
print("\nstorage - report records")
# --------------------------------------------------------------------------- #

TENANT = "check-clinic"


def report_suite(store, label: str) -> None:
    print(f"\n{label} — {store.describe()}")
    a = {"id": "aaa111", "status": "draft", "urgency": "routine",
         "patient_key": "sunita-devi-f", "report_text": "one", "updated": "2026-08-01"}
    b = {"id": "bbb222", "status": "signed", "urgency": "stat",
         "patient_key": "sunita-devi-f", "report_text": "two", "updated": "2026-08-02"}
    c = {"id": "ccc333", "status": "signed", "urgency": "urgent",
         "patient_key": "other-patient-m", "report_text": "three", "updated": "2026-08-03"}
    for record in (a, b, c):
        store.save_report(TENANT, record)

    check(store.get_report(TENANT, "aaa111")["report_text"] == "one",
          "a record survives the round trip")
    check(store.get_report(TENANT, "zzz") is None, "a missing record is None, not a crash")
    check(store.get_report("other-tenant", "aaa111") is None,
          "another tenant cannot read the record")

    check({r["id"] for r in store.list_reports(TENANT)} == {"aaa111", "bbb222", "ccc333"},
          "list returns everything for the tenant")
    check({r["id"] for r in store.list_reports(TENANT, status="signed")}
          == {"bbb222", "ccc333"}, "status filter works")
    check({r["id"] for r in store.list_reports(TENANT, patient_key="sunita-devi-f")}
          == {"aaa111", "bbb222"}, "patient filter works")

    a["status"] = "signed"
    store.save_report(TENANT, a)
    check(store.get_report(TENANT, "aaa111")["status"] == "signed",
          "saving again updates in place")

    check(store.delete_report(TENANT, "ccc333"), "delete reports success")
    check(store.get_report(TENANT, "ccc333") is None, "deleted means gone")
    check(not store.delete_report(TENANT, "ccc333"), "double delete reports failure")


tmp = tempfile.mkdtemp(prefix="hc-clinical-check-")
try:
    report_suite(storage.FileStore(os.path.join(tmp, "files")), "FileStore")
    report_suite(storage.SqlStore("sqlite:///" + os.path.join(tmp, "check.db").replace("\\", "/")),
                 "SqlStore (SQLite)")

    # worklist ordering through the records API, against an injected store
    storage._store = storage.FileStore(os.path.join(tmp, "worklist"))
    for text, study in (
        ("CT BRAIN\n\nFINDINGS:\nAcute subdural hematoma with midline shift.\n\n"
         "IMPRESSION:\n- Subdural.", "stat case"),
        ("USG ABDOMEN\n\nFINDINGS:\nLiver is normal.\n\n"
         "IMPRESSION:\n- Normal study.", "routine case"),
        ("CT ABDOMEN\n\nFINDINGS:\nFindings of acute appendicitis.\n\n"
         "IMPRESSION:\n- Appendicitis.", "urgent case"),
    ):
        parsed = parse_report(text)
        rec = records.new_record(text, parsed.blocks)
        records.save(rec, tenant="check-clinic")
    ordered = records.worklist(tenant="check-clinic")
    check([r["urgency"] for r in ordered] == ["stat", "urgent", "routine"],
          f"the worklist sorts stat first ({[r['urgency'] for r in ordered]})")

    # priors: only signed/delivered reports of the same patient come back
    storage._store = storage.FileStore(os.path.join(tmp, "priors"))
    first_text = ("USG THYROID\n\nPATIENT NAME: Asha Rao\nAGE/SEX: 40Y/F\n\n"
                  "FINDINGS:\nA nodule measures 8 mm in the right thyroid lobe.\n\n"
                  "IMPRESSION:\n- Nodule.")
    parsed = parse_report(first_text)
    old_rec = records.new_record(first_text, parsed.blocks)
    records.sign(old_rec, user="dr-a")
    records.save(old_rec, tenant="check-clinic")

    draft_text = first_text.replace("8 mm", "9 mm")
    draft_rec = records.new_record(draft_text, parse_report(draft_text).blocks)
    records.save(draft_rec, tenant="check-clinic")

    new_text = ("USG THYROID\n\nPATIENT NAME: Mrs Asha Rao\nAGE/SEX: 41 Y / F\n\n"
                "FINDINGS:\nA nodule measures 15 mm in the right thyroid lobe.\n\n"
                "IMPRESSION:\n- Nodule, larger.")
    new_rec = records.new_record(new_text, parse_report(new_text).blocks)
    priors = records.priors(new_rec, tenant="check-clinic")
    check(len(priors) == 1 and priors[0]["id"] == old_rec["id"],
          f"only the signed prior of the same patient comes back ({len(priors)})")
finally:
    storage._store = None
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All clinical checks passed.")
