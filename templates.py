"""
Doctor-wise report templates.

Everything the HC FORMAT hardcoded - font, size, colour, line spacing, margins,
and the bold/italic/underline/alignment of every kind of line - lives in a
Template. One template per doctor, saved as JSON in `templates/`, so a report
generated for Dr. Sharad comes out in Dr. Sharad's house style without anyone
touching a setting.

`HC_FORMAT` is the built-in default and is exactly the format signed off with
the client. New templates start as a copy of it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field, replace

import storage

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

# Every kind of line the engine can render.
BLOCK_KINDS = (
    "title",
    "heading",
    "heading_inline",
    "subheading",
    "bullet",
    "bold_bullet",
    "text",
    "verbatim",
)

ALIGNMENTS = ("left", "center", "right", "justify")

# Caps on everything the app learns, so a doctor's prompt cannot grow without limit.
MAX_EXAMPLES = 8  # few-shot prompts stop improving well before this
MAX_CORRECTIONS = 12
MAX_PREFERENCES = 40  # an uncapped rule list grows the prompt without limit
MAX_VOCABULARY = 300
MAX_DICTATION_FIXES = 60


@dataclass
class BlockStyle:
    """How one kind of line is rendered."""

    bold: bool = False
    italic: bool = False
    underline: bool = False
    align: str = "left"
    uppercase: bool = False
    bullet: bool = False
    space_before: float = 0.0  # points
    space_after: float = 4.0  # points
    font_size: float | None = None  # None -> the template's size
    font_name: str | None = None  # None -> the template's font


@dataclass
class Correction:
    """One thing the doctor changed in an AI draft.

    Kept as the pair the doctor actually produced, not as a guess about why.
    The 'why' is distilled separately into `Template.preferences`, which is
    what the next prompt reads.
    """

    before: str = ""
    after: str = ""
    note: str = ""
    when: str = ""


# Built-in category SUGGESTIONS - not a whitelist. A doctor can type their
# own ("Interventional", "Cardiac CT") and it sticks; clean_category() only
# rejects emptiness and absurd length.
CATEGORIES = ("General", "USG", "CT", "MRI", "X-Ray", "Mammography",
              "Doppler", "Fluoroscopy", "Nuclear")


def clean_category(value) -> str:
    """Any sane non-empty name survives; junk falls back to General."""
    cleaned = re.sub(r"[^\w /&.-]", "", str(value or "")).strip()[:24]
    return cleaned or "General"


def known_categories(all_templates: dict | None = None) -> list[str]:
    """The built-ins plus every custom category already in use, for pickers."""
    seen = list(CATEGORIES)
    for tpl in (all_templates or {}).values():
        category = getattr(tpl, "category", "") or ""
        if category and category not in seen:
            seen.append(category)
    return seen


@dataclass
class Template:
    name: str = "HC FORMAT (default)"
    doctor: str = ""
    # Which study family this template is for (one of CATEGORIES), and who
    # made it, when - so a clinic with thirty templates can tell "Dr. Sharma's
    # CT brain" from "Dr. Sharma's MRI spine" at a glance.
    category: str = "General"
    created_by: str = ""
    created_at: str = ""
    font_name: str = "Arial"
    font_size: float = 12.0
    font_color: str = "000000"
    line_spacing: float = 1.5
    margin_top: float = 1.0
    margin_bottom: float = 1.0
    margin_left: float = 1.0
    margin_right: float = 1.0
    page_numbers: bool = False
    # Letterhead carried by the doctor's template, so it loads automatically.
    letterhead_name: str = ""
    letterhead_address: str = ""
    letterhead_contact: str = ""
    # Lines appended at the end of every report for this doctor.
    signoff: list[str] = field(default_factory=list)
    styles: dict[str, BlockStyle] = field(default_factory=dict)
    # Past reports in this doctor's own words, used as few-shot examples when
    # drafting rough notes into their signature style. Prose, not formatting.
    examples: list[str] = field(default_factory=list)
    style_notes: str = ""
    # What the doctor changed in past drafts, and the short rules distilled from
    # those changes. This is how the app gets better at one doctor over time.
    corrections: list[Correction] = field(default_factory=list)
    preferences: list[str] = field(default_factory=list)
    # Dictation: terms this doctor actually says, and words the transcriber has
    # got wrong before. Both are fed to the next transcription so the same
    # mishearing does not happen twice.
    vocabulary: list[str] = field(default_factory=list)
    dictation_fixes: list[Correction] = field(default_factory=list)
    # Answers the doctor has already given to clarifying questions, so the same
    # question is not asked twice.
    answered: dict[str, str] = field(default_factory=dict)
    # Macros: a short dictated trigger (".normalchest") that expands into a
    # complete block of this doctor's standard text. Per-doctor, on top of the
    # built-in defaults in BUILTIN_MACROS.
    macros: dict[str, str] = field(default_factory=dict)
    builtin: bool = False

    def style(self, kind: str) -> BlockStyle:
        return self.styles.get(kind) or self.styles.get("text") or BlockStyle()

    @property
    def signoff_text(self) -> str:
        return " ".join(self.signoff)


def _hc_format() -> Template:
    """The signed-off HC FORMAT, expressed as a template."""
    return Template(
        name="HC FORMAT (default)",
        builtin=True,
        styles={
            # Rule 3: centred, bold, underlined, uppercase.
            "title": BlockStyle(bold=True, underline=True, align="center",
                                uppercase=True, space_after=12),
            # Rule 4: left, bold, underlined, uppercase.
            "heading": BlockStyle(bold=True, underline=True, uppercase=True,
                                  space_before=10, space_after=4),
            # Rule 4 on the label only - the value run stays plain.
            "heading_inline": BlockStyle(bold=True, underline=True, uppercase=True,
                                         space_before=4, space_after=2),
            # Rule 5: italic + underlined, explicitly NOT bold.
            "subheading": BlockStyle(italic=True, underline=True,
                                     space_before=6, space_after=2),
            "bullet": BlockStyle(bullet=True, space_after=3),
            # Rule 6: every impression bullet bold.
            "bold_bullet": BlockStyle(bullet=True, bold=True, space_after=3),
            "text": BlockStyle(space_after=3),
            # As-is mode: nothing added, nothing taken away.
            "verbatim": BlockStyle(space_after=0),
        },
    )


HC_FORMAT = _hc_format()


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "template"


def path_for(name: str) -> str:
    """
    Filename for a template.

    The slug alone is not enough: "Dr. Sharad" and "Dr Sharad!!" both slugify to
    "dr-sharad", so one doctor would silently overwrite the other. A short digest
    of the exact name keeps distinct names in distinct files. The slug is capped
    so the path stays inside Windows' limit for a long name.
    """
    import hashlib

    digest = hashlib.sha1(name.strip().encode("utf-8")).hexdigest()[:8]
    return os.path.join(TEMPLATE_DIR, f"{_slug(name)[:60]}-{digest}.json")


def _find_file(name: str) -> str | None:
    """Locate a saved template by its stored name, whatever the file is called."""
    exact = path_for(name)
    if os.path.exists(exact):
        return exact
    if not os.path.isdir(TEMPLATE_DIR):
        return None
    for filename in os.listdir(TEMPLATE_DIR):
        if not filename.endswith(".json"):
            continue
        candidate = os.path.join(TEMPLATE_DIR, filename)
        if not os.path.isfile(candidate):
            continue
        try:
            with open(candidate, encoding="utf-8") as fh:
                if str(json.load(fh).get("name", "")).strip() == name.strip():
                    return candidate
        except Exception:
            continue
    return None


def to_dict(template: Template) -> dict:
    data = asdict(template)
    data["styles"] = {k: asdict(v) for k, v in template.styles.items()}
    data["corrections"] = [asdict(c) for c in template.corrections]
    data["dictation_fixes"] = [asdict(c) for c in template.dictation_fixes]
    return data


def _num(value, fallback: float, low: float, high: float) -> float:
    """Coerce a hand-edited JSON value to a usable number, or fall back."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return fallback
    if out != out or out in (float("inf"), float("-inf")):  # NaN / inf
        return fallback
    return min(max(out, low), high)


def _flag(value, fallback: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return fallback


def from_dict(data: dict) -> Template:
    """
    Build a Template from JSON.

    Templates are plain files a user may hand-edit, so every value is coerced
    rather than trusted: a font size of "big" or a null line spacing must give a
    usable document, not a traceback in the middle of a clinic.
    """
    if not isinstance(data, dict):
        raise ValueError("A template file must contain a JSON object.")

    raw_styles = data.get("styles")
    raw_styles = raw_styles if isinstance(raw_styles, dict) else {}
    styles = {}
    for kind in BLOCK_KINDS:
        base = HC_FORMAT.styles.get(kind, BlockStyle())
        given = raw_styles.get(kind)
        if not isinstance(given, dict):
            styles[kind] = replace(base)
            continue
        styles[kind] = BlockStyle(
            bold=_flag(given.get("bold"), base.bold),
            italic=_flag(given.get("italic"), base.italic),
            underline=_flag(given.get("underline"), base.underline),
            uppercase=_flag(given.get("uppercase"), base.uppercase),
            bullet=_flag(given.get("bullet"), base.bullet),
            align=(str(given.get("align") or base.align).lower()
                   if str(given.get("align") or base.align).lower() in ALIGNMENTS
                   else base.align),
            space_before=_num(given.get("space_before"), base.space_before, 0, 200),
            space_after=_num(given.get("space_after"), base.space_after, 0, 200),
            font_size=(None if given.get("font_size") in (None, "")
                       else _num(given.get("font_size"), 12.0, 4, 96)),
            font_name=str(given["font_name"]) if given.get("font_name") else None,
        )

    fields = {k: v for k, v in data.items() if k not in ("styles",)}
    fields.pop("builtin", None)
    signoff = fields.pop("signoff", None) or []
    examples = fields.pop("examples", None) or []
    preferences = fields.pop("preferences", None) or []
    answered = fields.pop("answered", None) or {}
    vocabulary = fields.pop("vocabulary", None) or []

    def _corrections(raw) -> list[Correction]:
        return [
            Correction(
                before=str(c.get("before", "")),
                after=str(c.get("after", "")),
                note=str(c.get("note", "")),
                when=str(c.get("when", "")),
            )
            for c in (raw or [])
            if isinstance(c, dict)
        ]

    corrections = _corrections(fields.pop("corrections", None))
    dictation_fixes = _corrections(fields.pop("dictation_fixes", None))

    def _texts(value) -> list[str]:
        """A hand-edited list may arrive as a bare string, or as junk."""
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, (list, tuple)):
            return [str(v) for v in value if str(v).strip()]
        return []

    return Template(
        name=str(fields.get("name") or "Untitled template").strip() or "Untitled template",
        doctor=str(fields.get("doctor") or "").strip(),
        category=clean_category(fields.get("category")),
        created_by=str(fields.get("created_by") or "").strip(),
        created_at=str(fields.get("created_at") or "").strip(),
        font_name=str(fields.get("font_name") or "Arial").strip() or "Arial",
        font_size=_num(fields.get("font_size"), 12.0, 4, 96),
        font_color=(str(fields.get("font_color") or "000000").lstrip("#").strip() or "000000"),
        line_spacing=_num(fields.get("line_spacing"), 1.5, 0.5, 5),
        margin_top=_num(fields.get("margin_top"), 1.0, 0.1, 5),
        margin_bottom=_num(fields.get("margin_bottom"), 1.0, 0.1, 5),
        margin_left=_num(fields.get("margin_left"), 1.0, 0.1, 5),
        margin_right=_num(fields.get("margin_right"), 1.0, 0.1, 5),
        page_numbers=_flag(fields.get("page_numbers")),
        letterhead_name=str(fields.get("letterhead_name") or ""),
        letterhead_address=str(fields.get("letterhead_address") or ""),
        letterhead_contact=str(fields.get("letterhead_contact") or ""),
        style_notes=str(fields.get("style_notes") or ""),
        signoff=_texts(signoff),
        examples=_texts(examples)[-MAX_EXAMPLES:],
        corrections=corrections[-MAX_CORRECTIONS:],
        preferences=_texts(preferences)[-MAX_PREFERENCES:],
        answered={str(k): str(v) for k, v in dict(answered).items()}
        if isinstance(answered, dict) else {},
        macros={str(k).strip(): str(v) for k, v in dict(fields.get("macros") or {}).items()
                if str(k).strip() and str(v).strip()}
        if isinstance(fields.get("macros"), dict) else {},
        vocabulary=_texts(vocabulary)[-MAX_VOCABULARY:],
        dictation_fixes=dictation_fixes[-MAX_DICTATION_FIXES:],
        styles=styles,
        builtin=False,
    )


# Re-exported so callers catch templates.ConflictError without importing storage.
ConflictError = storage.ConflictError

# Persistence is delegated to storage.py, which decides between JSON files,
# SQLite and Postgres from STORAGE_URL. Everything below stays identical to the
# caller whichever one is in use.


def fingerprint(name: str, tenant: str | None = None) -> str:
    """Identity of the stored version, for detecting a concurrent edit."""
    return storage.get_store().fingerprint(tenant or storage.current_tenant(), name)


def save(template: Template, *, expect: str | None = None,
         tenant: str | None = None) -> str:
    """
    Write a template.

    Pass `expect` (a fingerprint taken when the form was drawn) to refuse the
    write if someone else changed the same template in the meantime.
    """
    if template.name.strip() in ("__clinic_corpus__", "__agent_memory__",
                                 "__preference_pairs__"):
        # corpus.py and memory.py store their records under these names in
        # the same store; a template wearing one would shadow them.
        raise ValueError("That name is reserved for the app's own records.")
    storage.get_store().save(
        tenant or storage.current_tenant(), template.name, to_dict(template), expect=expect
    )
    storage.log("template.saved", template.name,
                f"{template.font_name} {template.font_size:g}pt, "
                f"{len(template.examples)} example(s), {len(template.preferences)} rule(s)")
    return template.name


def delete(name: str, tenant: str | None = None) -> bool:
    removed = storage.get_store().delete(tenant or storage.current_tenant(), name)
    if removed:
        storage.log("template.deleted", name)
    return removed


def rename(old_name: str, template: Template, tenant: str | None = None) -> str:
    """Save under the new name first, then drop the old record."""
    written = save(template, tenant=tenant)
    if old_name.strip() != template.name.strip():
        storage.get_store().delete(tenant or storage.current_tenant(), old_name)
        storage.log("template.renamed", template.name, f"was {old_name}")
    return written


def load_all(tenant: str | None = None) -> dict[str, Template]:
    """Built-in first, then every template belonging to this tenant."""
    out: dict[str, Template] = {HC_FORMAT.name: _hc_format()}
    scope = tenant or storage.current_tenant()
    for name, payload in storage.get_store().load_all(scope).items():
        if name == HC_FORMAT.name:
            continue
        if name in ("__clinic_corpus__", "__agent_memory__",
                    "__preference_pairs__"):
            # corpus.py and memory.py ride the same store under these
            # reserved names; none of them is a template. Exact match on
            # purpose.
            continue
        try:
            out[name] = from_dict(payload)
        except Exception:
            continue  # one bad record must not hide the rest
    return out


def copy_of(template: Template, new_name: str, doctor: str = "") -> Template:
    fresh = from_dict(to_dict(template))
    fresh.name = new_name
    fresh.doctor = doctor or template.doctor
    fresh.builtin = False
    return fresh


# --------------------------------------------------------------------------- #
# Learning
# --------------------------------------------------------------------------- #



def _now() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M")


def remember_example(template: Template, report: str) -> Template:
    """Keep a signed report as a voice sample. Newest wins when the cap is hit."""
    fresh = copy_of(template, template.name)
    text = report.strip()
    if text and text not in fresh.examples:
        fresh.examples = (fresh.examples + [text])[-MAX_EXAMPLES:]
    return fresh


def remember_correction(
    template: Template, before: str, after: str, note: str = "", rules: list[str] | None = None
) -> Template:
    """
    Record what the doctor changed, plus any rules distilled from it.

    Rules are deduplicated case-insensitively so the same lesson learned twice
    does not weight the prompt twice.
    """
    fresh = copy_of(template, template.name)
    if before.strip() != after.strip():
        fresh.corrections = (
            fresh.corrections
            + [Correction(before=before.strip(), after=after.strip(), note=note.strip(), when=_now())]
        )[-MAX_CORRECTIONS:]

    # Dedupe on a normalised key but store the rule exactly as written - trimming
    # the text itself would quietly rewrite what the doctor is being taught.
    def key(rule: str) -> str:
        return rule.strip().rstrip(".").lower()

    seen = {key(p) for p in fresh.preferences}
    for rule in rules or []:
        clean = rule.strip()
        if clean and key(clean) not in seen:
            fresh.preferences.append(clean)
            seen.add(key(clean))
    fresh.preferences = fresh.preferences[-MAX_PREFERENCES:]
    return fresh


def remember_answer(template: Template, question: str, answer: str) -> Template:
    """Never ask a doctor the same clarifying question twice."""
    fresh = copy_of(template, template.name)
    if question.strip() and answer.strip():
        fresh.answered[question.strip()] = answer.strip()
    return fresh


def remember_vocabulary(template: Template, terms: list[str]) -> Template:
    """Add terms this doctor actually says, so the transcriber expects them."""
    fresh = copy_of(template, template.name)
    seen = {t.strip().lower() for t in fresh.vocabulary}
    for term in terms:
        clean = term.strip()
        if clean and clean.lower() not in seen:
            fresh.vocabulary.append(clean)
            seen.add(clean.lower())
    fresh.vocabulary = fresh.vocabulary[-MAX_VOCABULARY:]
    return fresh


def remember_dictation_fix(template: Template, heard: str, correct: str) -> Template:
    """
    Record a mishearing so it is not repeated.

    Stored as heard -> correct pairs and shown to the next transcription. This
    is what makes dictation get better at one doctor's voice and vocabulary.
    """
    fresh = copy_of(template, template.name)
    heard, correct = heard.strip(), correct.strip()
    if not heard or not correct or heard.lower() == correct.lower():
        return fresh
    already = {(c.before.lower(), c.after.lower()) for c in fresh.dictation_fixes}
    if (heard.lower(), correct.lower()) not in already:
        fresh.dictation_fixes = (
            fresh.dictation_fixes + [Correction(before=heard, after=correct, when=_now())]
        )[-MAX_DICTATION_FIXES:]
    return remember_vocabulary(fresh, [correct])


def forget_vocabulary(template: Template, term: str) -> Template:
    fresh = copy_of(template, template.name)
    fresh.vocabulary = [t for t in fresh.vocabulary if t != term]
    return fresh


def forget_preference(template: Template, rule: str) -> Template:
    fresh = copy_of(template, template.name)
    fresh.preferences = [p for p in fresh.preferences if p != rule]
    return fresh


def learning_summary(template: Template) -> str:
    bits = []
    if template.examples:
        bits.append(f"{len(template.examples)} report(s)")
    if template.preferences:
        bits.append(f"{len(template.preferences)} learned rule(s)")
    if template.corrections:
        bits.append(f"{len(template.corrections)} correction(s)")
    if template.answered:
        bits.append(f"{len(template.answered)} answered question(s)")
    if template.vocabulary:
        bits.append(f"{len(template.vocabulary)} dictation term(s)")
    if template.dictation_fixes:
        bits.append(f"{len(template.dictation_fixes)} mishearing(s) fixed")
    if template.macros:
        bits.append(f"{len(template.macros)} macro(s)")
    return " · ".join(bits) or "nothing learned yet"


# --------------------------------------------------------------------------- #
# Macros - dictated triggers that expand into standard text
# --------------------------------------------------------------------------- #

# Defaults every doctor gets. A doctor's own macro with the same trigger wins.
# Triggers start with "." so ordinary prose can never expand by accident.
BUILTIN_MACROS: dict[str, str] = {
    ".normalchest": (
        "FINDINGS:\n"
        "Both lung fields are clear. No focal parenchymal lesion.\n"
        "Cardiac size and mediastinal contours are within normal limits.\n"
        "Both costophrenic angles are free. No pleural effusion or pneumothorax.\n"
        "Visualised bony thorax and soft tissues are unremarkable.\n\n"
        "IMPRESSION:\n"
        "No significant abnormality detected in the present chest radiograph."
    ),
    ".normalabdomen": (
        "FINDINGS:\n"
        "Liver is normal in size and echotexture. No focal lesion.\n"
        "Gallbladder is normal. No calculus. CBD is not dilated.\n"
        "Pancreas and spleen are normal.\n"
        "Both kidneys are normal in size, shape and echotexture. "
        "No calculus or hydronephrosis.\n"
        "Urinary bladder is normal. No free fluid in the abdomen.\n\n"
        "IMPRESSION:\n"
        "No significant abnormality detected in the present study."
    ),
    ".normalbrainct": (
        "FINDINGS:\n"
        "Brain parenchyma shows normal attenuation. No focal lesion.\n"
        "No evidence of intracranial haemorrhage, mass effect or midline shift.\n"
        "Ventricular system and basal cisterns are normal.\n"
        "Visualised paranasal sinuses and bony calvarium are unremarkable.\n\n"
        "IMPRESSION:\n"
        "No significant abnormality detected in the present CT study of the brain."
    ),
}

_MACRO_TRIGGER = re.compile(r"(?<![\w.])\.[a-z][a-z0-9_]{2,}\b")


def expand_macros(text: str, template: Template | None = None) -> tuple[str, list[str]]:
    """
    Expand every macro trigger in the text. Returns (expanded, triggers_used).

    The doctor's own macros override the built-ins. Unknown triggers are left
    exactly as typed - flagging them is validate.py's placeholder check's job,
    and silently deleting text is nobody's job.
    """
    table = dict(BUILTIN_MACROS)
    if template is not None:
        for trigger, body in (template.macros or {}).items():
            key = trigger if trigger.startswith(".") else f".{trigger}"
            table[key.lower()] = body

    used: list[str] = []

    def replace(match: re.Match) -> str:
        trigger = match.group(0).lower()
        if trigger in table:
            used.append(trigger)
            return table[trigger]
        return match.group(0)

    return _MACRO_TRIGGER.sub(replace, text), used


def macro_trigger(name: str) -> str:
    """
    A macro trigger derived from a template's name: "Dr. Sharma CT Brain" ->
    ".ctbrainsharma"-ish is unreadable, so keep it simple: lowercase letters
    and digits of the name, dot-prefixed, capped. "MRI Brain" -> ".mribrain".
    """
    stem = re.sub(r"[^a-z0-9]", "", (name or "").lower())[:24]
    return f".{stem or 'template'}"


def stamp_creation(template: Template, created_by: str = "") -> Template:
    """Record who made this template and when, once, at creation."""
    from datetime import datetime, timezone

    if not template.created_at:
        template.created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if created_by and not template.created_by:
        template.created_by = created_by
    return template


def remember_macro(template: Template, trigger: str, body: str) -> Template:
    """Store one macro on the doctor's template. Trigger is normalised to '.x'."""
    key = trigger.strip().lower()
    if not key.startswith("."):
        key = f".{key}"
    if len(key) < 4:
        raise ValueError("A macro trigger needs at least three letters after the dot.")
    if not body.strip():
        raise ValueError("A macro needs body text to expand into.")
    updated = copy_of(template, template.name, doctor=template.doctor)
    updated.macros = dict(template.macros or {})
    updated.macros[key] = body.strip()
    return updated


def forget_macro(template: Template, trigger: str) -> Template:
    updated = copy_of(template, template.name, doctor=template.doctor)
    updated.macros = {k: v for k, v in (template.macros or {}).items()
                      if k != trigger.strip().lower()}
    return updated
