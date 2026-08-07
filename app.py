"""
HC FORMAT Radiology Report Generator.

Paste (or upload) whatever the boss sent, get back a Microsoft Word .docx that
follows the HC FORMAT exactly, with a word-loss audit proving nothing was
changed, summarised or dropped.

Run:  streamlit run app.py
"""

from __future__ import annotations

import io
import os
import re
import types
import zipfile
from dataclasses import asdict
from datetime import datetime

import streamlit as st
import streamlit.components.v1 as components

import access
import dictation_fix
import readers
import storage
import templates
import validate
from hc_format import Block, ParseOptions, Span, build_docx, parse_report
from verify import audit

try:
    import speech

    SPEECH_IMPORT_ERROR = ""
    SpeechError = speech.SpeechError
except Exception as _speech_exc:  # pragma: no cover - only on a broken install
    # Dictation still works on Gemini without this module, so a problem loading it
    # must not take the tab down. The Speech engine panel explains what is wrong.
    speech = None
    SPEECH_IMPORT_ERROR = str(_speech_exc)

    class SpeechError(RuntimeError):
        """Stand-in so the dictation handlers stay valid without the module."""

st.set_page_config(page_title="HC Format Radiology Report Generator", layout="wide", page_icon=":material/clinical_notes:")

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #

# Gate before anything renders. With no ACCESS_CODE and no [auth] this is a
# no-op, so the clinic LAN is unaffected.
if not access.require_access():
    st.stop()

st.sidebar.title("Settings")

engine = st.sidebar.radio(
    "Engine",
    ["Rule-based (offline, 0% word loss)", "AI-assisted (Gemini)"],
    help=(
        "**Rule-based** needs no API key and no internet. It only applies styling, so it "
        "cannot alter a single word. It handles almost every report.\n\n"
        "**AI-assisted** turns on three things that need a free Gemini key:\n"
        "- laying out text too messy for the heading rules to read\n"
        "- OCR, so a scanned PDF or a photo of a report can be used\n"
        "- the *Draft in doctor's style* tab\n\n"
        "Formatting stays safe either way: the AI only classifies lines, the word-loss "
        "audit still runs, and any drift falls back to the rule-based result "
        "automatically. Only the drafting tab rewrites words, and it says so."
    ),
)
use_ai = engine.startswith("AI")

if use_ai:
    st.sidebar.caption("Unlocks messy-text layout, OCR of scans and photos, and AI drafting.")

PLACEHOLDERS = {"paste-your-key-here", "your-key", "your-key-here", "changeme", ""}


def configured_secret(name: str, default: str = "") -> str:
    """
    Read a setting from .streamlit/secrets.toml, falling back to the environment.

    The key is never typed into the app and never lives in the source. secrets.toml
    is gitignored; on Streamlit Cloud the same name goes in the Secrets panel, so
    the deployed app picks it up without the key ever entering the repository.
    """
    value = ""
    try:
        if name in st.secrets:
            value = str(st.secrets[name]).strip()
    except Exception:
        pass  # no secrets.toml at all - fall through to the environment
    value = value or os.environ.get(name, default).strip()
    # An untouched copy of secrets.toml.example must read as "not set up yet",
    # otherwise the first run fails with a confusing API error instead of
    # telling the user what to do.
    return "" if value in PLACEHOLDERS else value


MODEL_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".model_cache.json")
MODEL_CACHE_DAYS = 7


def fetch_models(key: str, force: bool = False) -> tuple[list[str], str, str]:
    """
    Model list, fetched once and kept on disk.

    Asking Google on every page load costs a round trip for a list that changes
    a few times a year, so it is cached and refreshed weekly. A failed refresh
    keeps serving the cached list rather than leaving the picker empty.
    """
    import json

    cached: dict = {}
    if os.path.exists(MODEL_CACHE):
        try:
            with open(MODEL_CACHE, encoding="utf-8") as fh:
                cached = json.load(fh)
        except Exception:
            cached = {}

    models = [str(m) for m in cached.get("models", [])]
    fetched_at = str(cached.get("fetched_at", ""))

    fresh = False
    if fetched_at:
        try:
            fresh = (datetime.now() - datetime.fromisoformat(fetched_at)).days < MODEL_CACHE_DAYS
        except Exception:
            fresh = False

    if models and fresh and not force:
        return models, fetched_at, ""

    try:
        import ai_parser

        models = ai_parser.list_models(key)
        fetched_at = datetime.now().isoformat(timespec="seconds")
        with open(MODEL_CACHE, "w", encoding="utf-8") as fh:
            json.dump({"models": models, "fetched_at": fetched_at}, fh, indent=2)
        return models, fetched_at, ""
    except Exception as exc:
        # Stale beats empty: a network hiccup must not empty the picker.
        return models, fetched_at, str(exc)


@st.cache_data(show_spinner=False)
def _cached_models(fingerprint: str, _key: str) -> tuple[list[str], str, str]:
    """Cached on a fingerprint, never on the key itself (leading underscore = not hashed)."""
    return fetch_models(_key)


def cached_models(key: str) -> tuple[list[str], str, str]:
    """Once per session on top of the on-disk cache, so reruns cost nothing.

    The API key is passed as `_key` so Streamlit does not hash or store it in the
    cache index; a short digest identifies the key instead.
    """
    import hashlib

    fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return _cached_models(fingerprint, key)


def pick_default_model(models: list[str]) -> str:
    """
    The newest usable model, without hardcoding a name that goes stale.

    Google publish `gemini-flash-latest`, an alias they keep pointed at the current
    flash model - so preferring it means this app is on the latest release the day
    it ships, with no code change. Failing that, take the highest version number
    among plain flash models, skipping the -lite, -image, -tts and -preview
    variants, which are either weaker or not general-purpose.
    """
    import re as _re

    if not models:
        return "gemini-flash-latest"

    for alias in ("gemini-flash-latest", "gemini-pro-latest"):
        if alias in models:
            return alias

    skip = ("-lite", "-image", "-tts", "-preview", "-exp", "-thinking",
            "computer-use", "robotics", "omni")
    versioned = []
    for name in models:
        match = _re.fullmatch(r"gemini-(\d+)(?:\.(\d+))?-(flash|pro)", name)
        if match and not any(bad in name for bad in skip):
            major, minor, family = match.group(1), match.group(2) or "0", match.group(3)
            versioned.append(((int(major), int(minor), family == "flash"), name))
    if versioned:
        return max(versioned)[1]

    gemini = [m for m in models if m.startswith("gemini")]
    return gemini[0] if gemini else models[0]


api_key = configured_secret("GEMINI_API_KEY")
model_choice = ""

if use_ai:
    if not api_key:
        st.sidebar.error("No Gemini key configured.")
        st.sidebar.caption(
            "Put it in `.streamlit/secrets.toml` as `GEMINI_API_KEY = \"...\"`, or set the "
            "`GEMINI_API_KEY` environment variable, then reload. See `secrets.toml.example`."
        )
    else:
        models, fetched_at, fetch_error = cached_models(api_key)
        if fetch_error:
            st.sidebar.warning(f"Could not refresh the model list: {fetch_error}")

        model_choice = st.session_state.get("last_model") or configured_secret(
            "GEMINI_MODEL"
        ) or pick_default_model(models)

        options = (models or [model_choice]) + ["Custom / type model ID"]
        options = list(dict.fromkeys(options))  # keep order, drop duplicates
        picked = st.sidebar.selectbox(
            "Model",
            options,
            index=options.index(model_choice) if model_choice in options else 0,
            help="Fetched once and remembered. It refreshes itself weekly, so a retired "
                 "model ID can never quietly break the app.",
        )
        if picked == "Custom / type model ID":
            model_choice = st.sidebar.text_input("Model ID", value=model_choice).strip()
        else:
            model_choice = picked
        st.session_state["last_model"] = model_choice

        if models:
            newest = pick_default_model(models)
            pinned = configured_secret("GEMINI_MODEL")
            if pinned and pinned != newest:
                st.sidebar.caption(
                    f"Pinned to `{pinned}` by GEMINI_MODEL in secrets.toml. "
                    f"Remove that line to track the latest (`{newest}`)."
                )
            else:
                st.sidebar.caption(f"{len(models)} models available · tracking the latest")
        if st.sidebar.button("Refresh model list", help="Only needed if Google has just "
                                                        "released a model you want today."):
            fetch_models(api_key, force=True)
            _cached_models.clear()
            st.rerun()

st.sidebar.divider()
st.sidebar.subheader("Doctor / template")

@st.cache_resource(show_spinner=False)
def _template_cache() -> dict:
    """Templates, loaded once per process rather than once per interaction.

    Streamlit reruns this whole script on every click, and load_all() is a
    network round trip to the database - about 70 ms each time, for data that
    changes a few times a week. Cached and explicitly invalidated on write.
    """
    return {"version": 0, "data": None}


def load_templates() -> dict:
    cache = _template_cache()
    tenant = storage.current_tenant()
    # Keyed by tenant so one clinic's cache can never be served to another.
    if cache["data"] is None or cache.get("tenant") != tenant:
        cache["tenant"] = tenant
        cache["data"] = templates.load_all(tenant)
    return cache["data"]


def templates_changed() -> None:
    """Call after any save, rename or delete."""
    _template_cache()["data"] = None


all_templates = load_templates()
template_names = list(all_templates.keys())
if st.session_state.get("tpl_pick") not in template_names:
    st.session_state["tpl_pick"] = template_names[0]

# The picker itself lives in the Report tab, where the format is actually chosen.
# Reading session_state here keeps every tab on the same template in one pass.
picked_template = st.session_state["tpl_pick"]
template = all_templates[picked_template]


def template_summary(t: templates.Template) -> str:
    return (
        f"{t.font_name} {t.font_size:g} pt · line spacing {t.line_spacing:g} · "
        f"margins {t.margin_left:g}″"
    )


st.sidebar.caption(
    f"**{picked_template}**"
    + (f" — {template.doctor}" if template.doctor else "")
    + "\n\n"
    + template_summary(template)
)

# Delete needs a little more room than the other two or its label wraps.
new_col, edit_col, del_col = st.sidebar.columns([1, 1, 1.25])


@st.dialog("New doctor template")
def _dialog_new():
    st.write("Start from an existing template and give it the doctor's name.")
    base_name = st.selectbox("Copy settings from", template_names, key="new_base")
    name = st.text_input("Template name", value="", placeholder="Dr. Sharad", key="new_name")
    doctor = st.text_input("Doctor", value="", placeholder="Dr. Sharad Kulkarni", key="new_doctor")
    if st.button("Create", type="primary", use_container_width=True):
        if not name.strip():
            st.error("Give the template a name.")
        elif name.strip() in all_templates:
            st.error(f"“{name.strip()}” already exists — pick another name.")
        else:
            templates.save(templates.copy_of(all_templates[base_name], name.strip(), doctor.strip()))
            templates_changed()
            st.session_state["tpl_pick"] = name.strip()
            st.rerun()


@st.dialog("Edit template", width="large")
def _dialog_edit(current: templates.Template):
    if current.builtin:
        st.warning(
            "**HC FORMAT (default)** is the signed-off format and is read-only. "
            "Use ➕ New to start a doctor's template from it."
        )
        return

    st.caption("Per-line formatting and style examples live in the **Doctor templates** tab.")
    c1, c2 = st.columns(2)
    with c1:
        name = st.text_input("Template name", value=current.name, key="ed_name")
        doctor = st.text_input("Doctor", value=current.doctor, key="ed_doctor")
        font = st.text_input("Font", value=current.font_name, key="ed_font")
        size = st.number_input("Font size (pt)", 6.0, 36.0, float(current.font_size), 0.5,
                               key="ed_size")
        colour = st.text_input("Font colour (hex)", value=current.font_color, key="ed_colour")
    with c2:
        spacing = st.number_input("Line spacing", 1.0, 3.0, float(current.line_spacing), 0.05,
                                  key="ed_spacing")
        m1, m2 = st.columns(2)
        with m1:
            top = st.number_input("Top (in)", 0.2, 3.0, float(current.margin_top), 0.1, key="ed_top")
            left = st.number_input("Left (in)", 0.2, 3.0, float(current.margin_left), 0.1,
                                   key="ed_left")
        with m2:
            bottom = st.number_input("Bottom (in)", 0.2, 3.0, float(current.margin_bottom), 0.1,
                                     key="ed_bottom")
            right = st.number_input("Right (in)", 0.2, 3.0, float(current.margin_right), 0.1,
                                    key="ed_right")
        pages = st.checkbox("Page numbers by default", value=current.page_numbers, key="ed_pages")

    st.markdown("**Letterhead**")
    l1, l2, l3 = st.columns(3)
    with l1:
        lh_name = st.text_input("Clinic name", value=current.letterhead_name, key="ed_lh_name")
    with l2:
        lh_addr = st.text_input("Address", value=current.letterhead_address, key="ed_lh_addr")
    with l3:
        lh_contact = st.text_input("Phone / email", value=current.letterhead_contact,
                                   key="ed_lh_contact")

    if st.button("Save changes", type="primary", use_container_width=True):
        updated = templates.copy_of(current, name.strip() or current.name, doctor.strip())
        updated.font_name, updated.font_size, updated.font_color = font, float(size), colour
        updated.line_spacing = float(spacing)
        updated.margin_top, updated.margin_bottom = float(top), float(bottom)
        updated.margin_left, updated.margin_right = float(left), float(right)
        updated.page_numbers = bool(pages)
        updated.letterhead_name, updated.letterhead_address = lh_name, lh_addr
        updated.letterhead_contact = lh_contact
        # save-then-delete: losing the old file before the new one is written
        # would take the doctor's whole learned history with it.
        try:
            templates.rename(current.name, updated)
            templates_changed()
        except templates.ConflictError as exc:
            st.error(str(exc))
        else:
            st.session_state["tpl_pick"] = updated.name
            st.rerun()


@st.dialog("Set up a new doctor from this report")
def _dialog_seed_template(report_text: str, corrections: tuple[str, str] | None = None):
    """
    Create a doctor's whole environment in one step.

    The report becomes their first voice sample, so the very next draft already
    sounds like them instead of starting from nothing.
    """
    st.write("The report you just finished becomes this doctor's first voice sample.")
    base_name = st.selectbox("Formatting based on", template_names, key="seed_base")
    name = st.text_input("Template name", placeholder="Dr. Sharad", key="seed_name")
    doctor = st.text_input("Doctor", placeholder="Dr. Sharad Kulkarni", key="seed_doctor")
    notes = st.text_area(
        "Anything they always do (optional)",
        key="seed_notes",
        height=70,
        placeholder="Number the impression. Always close with 'Please correlate clinically.'",
    )
    st.caption(f"Seeding with {len(report_text.split())} words of their writing.")

    if st.button("Create doctor", type="primary", use_container_width=True):
        if not name.strip():
            st.error("Give the template a name.")
        elif name.strip() in all_templates:
            st.error(f"“{name.strip()}” already exists — pick another name.")
        else:
            fresh = templates.copy_of(all_templates[base_name], name.strip(), doctor.strip())
            fresh.style_notes = notes.strip()
            # A brand new doctor inherits formatting, never another doctor's voice.
            fresh.examples, fresh.corrections, fresh.preferences, fresh.answered = [], [], [], {}
            fresh = templates.remember_example(fresh, report_text)
            if corrections and corrections[0].strip() != corrections[1].strip():
                fresh = templates.remember_correction(fresh, corrections[0], corrections[1])
            templates.save(fresh)
            templates_changed()
            st.session_state["tpl_pick"] = fresh.name
            st.rerun()


@st.dialog("Delete template")
def _dialog_delete(current: templates.Template):
    if current.builtin:
        st.warning("**HC FORMAT (default)** is the built-in format and cannot be deleted.")
        return
    st.write(f"Delete **{current.name}**"
             + (f" ({current.doctor})" if current.doctor else "") + " permanently?")
    if current.examples:
        st.caption(f"Its {len(current.examples)} style example(s) go with it.")
    yes, no = st.columns(2)
    with yes:
        if st.button("Delete", type="primary", use_container_width=True):
            templates.delete(current.name)
            templates_changed()
            st.session_state["tpl_pick"] = templates.HC_FORMAT.name
            st.rerun()
    with no:
        if st.button("Cancel", use_container_width=True):
            st.rerun()


with new_col:
    if st.button(":material/add: New", use_container_width=True, help="Create a template for another doctor"):
        _dialog_new()
with edit_col:
    if st.button(":material/edit: Edit", use_container_width=True, help="Rename or change this template"):
        _dialog_edit(template)
with del_col:
    if st.button(":material/delete: Delete", use_container_width=True, help="Delete this template"):
        _dialog_delete(template)

access.sign_out_control()

problem = storage.storage_problem()
if problem:
    st.sidebar.error(problem, icon=":material/database_off:")

st.sidebar.divider()
st.sidebar.subheader("Parsing")

preserve_as_is = st.sidebar.checkbox(
    "Print exactly as pasted (as-is mode)",
    value=False,
    help="Keeps the original line breaks, blank lines and indentation and adds nothing. "
         "No bullets, no upper-casing, no heading detection. Use when the text is already "
         "formatted the way it should print.",
)

if preserve_as_is:
    opts = ParseOptions(preserve_as_is=True)
    st.sidebar.info("As-is mode: the font, size, spacing and margins still come from the template.")
else:
    opts = ParseOptions(
        split_sentences=st.sidebar.checkbox(
            "Split long findings paragraphs into one bullet per sentence",
            value=False,
            help="Off by default. Wording is identical either way - only the bullet split changes.",
        ),
        bold_comment_bullets=st.sidebar.checkbox(
            "Bullet + bold COMMENT / RECOMMENDATION too", value=False
        ),
        inline_meta_headings=st.sidebar.checkbox(
            "Keep PATIENT NAME / AGE/SEX on one line with their value",
            value=True,
            help="On: 'PATIENT NAME: Mr. X' with only the label bold + underlined. "
                 "Off: label and value on separate paragraphs.",
        ),
        max_subheading_len=st.sidebar.slider(
            "Organ subheading max length (characters)", 20, 80, 45,
            help="A short FINDINGS line under this length with no full stop is treated as an "
                 "organ subheading (italic + underlined, not bold).",
        ),
    )

st.sidebar.divider()
st.sidebar.subheader("Letterhead (optional)")

use_letterhead = st.sidebar.checkbox(
    "Add clinic / hospital letterhead",
    value=bool(template.letterhead_name or template.letterhead_address),
)
letterhead: dict = {}
letterhead_text = ""
if use_letterhead:
    letterhead["name"] = st.sidebar.text_input("Clinic name", value=template.letterhead_name)
    letterhead["address"] = st.sidebar.text_area(
        "Address", value=template.letterhead_address, height=70
    )
    letterhead["contact"] = st.sidebar.text_input(
        "Phone / email", value=template.letterhead_contact
    )
    logo = st.sidebar.file_uploader("Logo", type=["png", "jpg", "jpeg"])
    if logo:
        letterhead["logo_bytes"] = logo.getvalue()
    letterhead_text = " ".join(
        str(letterhead.get(k) or "") for k in ("name", "address", "contact")
    )

page_numbers = st.sidebar.checkbox("Page numbers in footer", value=template.page_numbers)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_blocks(raw_text: str) -> tuple[list[Block], list[str], str]:
    """Return (blocks, warnings, engine_used). AI is verified; it never wins on drift."""
    rule_result = parse_report(raw_text, opts)

    if not use_ai:
        return rule_result.blocks, rule_result.warnings, "rule-based"

    if not api_key or not model_choice:
        return rule_result.blocks, rule_result.warnings + [
            "AI engine selected but no Gemini key is configured - used the rule-based parser."
        ], "rule-based"

    try:
        import ai_parser

        ai_blocks = ai_parser.structure_with_ai(raw_text, api_key, model_choice)
    except Exception as exc:
        return rule_result.blocks, rule_result.warnings + [
            f"AI call failed ({exc}) - used the rule-based parser."
        ], "rule-based"

    check = audit(raw_text, build_docx(ai_blocks))
    if check.ok:
        return ai_blocks, rule_result.warnings, f"AI ({model_choice})"
    return rule_result.blocks, rule_result.warnings + [
        "AI output failed the word-loss audit "
        f"({check.summary}) - fell back to the rule-based parser."
    ], "rule-based (AI rejected)"


@st.cache_data(show_spinner=False, max_entries=32)
def render_report(payload: str) -> tuple[bytes, dict]:
    """
    Build the .docx and audit it, cached on the exact inputs.

    Both cost real time - about 40 ms to render and 9 ms to read back and audit -
    and Streamlit reruns the script on every click. Keyed on the blocks, template
    and letterhead, so it recomputes only when the document genuinely changes.
    """
    import json as _json

    data = _json.loads(payload)
    blocks = [
        Block(
            kind=b["kind"], text=b["text"], trailer=b.get("trailer", ""),
            spans=[Span(**sp) for sp in b.get("spans", [])],
            trailer_spans=[Span(**sp) for sp in b.get("trailer_spans", [])],
        )
        for b in data["blocks"]
    ]
    tpl = templates.from_dict(data["template"]) if data["template"] else None
    docx = build_docx(
        blocks, template=tpl, letterhead=data["letterhead"] or None,
        page_numbers=data["page_numbers"],
    )
    report = audit(
        data["raw"], docx,
        letterhead_text=data["letterhead_text"],
        page_numbers=data["page_numbers"],
        preserve_as_is=data["as_is"],
    )
    return docx, {
        "ok": report.ok, "summary": report.summary,
        "missing": report.missing, "added": report.added,
        "numbers_ok": report.numbers_ok, "missing_numbers": report.missing_numbers,
        "source_tokens": report.source_tokens,
    }


def render_key(blocks: list[Block], raw: str, tpl, letterhead: dict,
               page_numbers: bool, as_is: bool, letterhead_text: str) -> str:
    """Everything that can change the output, as one cache key."""
    import json as _json
    from dataclasses import asdict as _asdict

    return _json.dumps({
        "blocks": [
            {"kind": b.kind, "text": b.text, "trailer": b.trailer,
             "spans": [_asdict(s) for s in b.spans],
             "trailer_spans": [_asdict(s) for s in b.trailer_spans]}
            for b in blocks
        ],
        "template": templates.to_dict(tpl) if tpl else None,
        "letterhead": {k: v for k, v in (letterhead or {}).items() if k != "logo_bytes"},
        "page_numbers": page_numbers,
        "as_is": as_is,
        "raw": raw,
        "letterhead_text": letterhead_text,
    }, sort_keys=True, default=str)


def safe_filename(title: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_") or "Radiology_Report"
    return f"{stem[:70]}_HC_Format.docx"


# --------------------------------------------------------------------------- #
# Live editing
# --------------------------------------------------------------------------- #

KIND_LABELS = {
    "title": "Title (centred, bold, underlined)",
    "heading": "Main heading (bold, underlined)",
    "heading_inline": "Heading + value on one line",
    "subheading": "Organ subheading (italic, underlined)",
    "bullet": "Finding bullet",
    "bold_bullet": "Impression bullet (bold)",
    "text": "Plain text",
    "verbatim": "As-is line (spacing preserved)",
}


def _spans(raw) -> list[Span]:
    out: list[Span] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "")
        if not text:
            continue
        out.append(
            Span(
                text=text,
                bold=bool(item.get("bold")),
                italic=bool(item.get("italic")),
                underline=bool(item.get("underline")),
            )
        )
    return out


def blocks_to_rows(blocks: list[Block]) -> list[dict]:
    def emphasis(spans: list[Span]) -> str:
        return "".join(
            f"{'b' if s.bold else ''}{'i' if s.italic else ''}{'u' if s.underline else ''}:{s.text}|"
            for s in spans
        )

    return [
        {
            "Format": KIND_LABELS.get(b.kind, KIND_LABELS["text"]),
            "Text": b.text,
            "Value": b.trailer,
            # Included so a formatting-only change still registers as an edit.
            "Emphasis": emphasis(b.spans) + emphasis(b.trailer_spans),
        }
        for b in blocks
    ]


_HERE = os.path.dirname(os.path.abspath(__file__))
_wysiwyg = components.declare_component("hc_format_editor", path=os.path.join(_HERE, "editor"))
# Live dictation: browser speech recognition for instant feedback while speaking,
# plus a recording of the same session for the accurate Gemini pass.
_live_dictate = components.declare_component("live_dictate", path=os.path.join(_HERE, "live_dictate"))


def structure_editor(blocks: list[Block], signature: str) -> list[Block]:
    """
    Edit the report directly on the formatted page.

    The component renders the same HC FORMAT the .docx will use - centred bold
    underlined title, bold underlined headings, italic underlined organ
    subheadings, bulleted findings, bold impression bullets - and every line is
    contenteditable. The ¶ button in the left gutter changes a line's format
    when detection guessed wrong.

    The component only rebuilds its DOM when `nonce` changes, so the reruns it
    triggers by sending edits back never move the caret. A new source report
    means a new signature, hence a new widget key and a fresh parse.
    """
    payload = [
        {
            "kind": b.kind,
            "text": b.text,
            "value": b.trailer,
            "spans": [asdict(s) for s in b.spans],
            "value_spans": [asdict(s) for s in b.trailer_spans],
        }
        for b in blocks
    ]
    edited = _wysiwyg(blocks=payload, nonce=signature, key=f"wysiwyg::{signature}", default=None)

    if not edited:
        return blocks

    out: list[Block] = []
    for item in edited:
        kind = str(item.get("kind") or "text")
        if kind not in KIND_LABELS:
            kind = "text"
        text = str(item.get("text") or "")
        value = str(item.get("value") or "")
        if kind != "verbatim":
            # As-is lines keep their indentation; every other kind is trimmed.
            text, value = text.strip(), value.strip()
            if not text and not value:
                continue
        if kind in ("title", "heading", "heading_inline"):
            text = text.upper()  # rules 3 and 4 are not negotiable
        if kind != "heading_inline":
            value = ""
        out.append(
            Block(
                kind=kind,
                text=text,
                trailer=value,
                raw=text,
                spans=_spans(item.get("spans")),
                trailer_spans=_spans(item.get("value_spans")),
            )
        )
    return out or blocks


def render_audit(result, *, user_edited: bool = False) -> None:
    """Show an audit result. The audit itself runs inside the cached render."""
    if isinstance(result, dict):
        result = types.SimpleNamespace(**result)
    if result.ok:
        st.success(result.summary)
        return

    if user_edited:
        st.warning(
            "Your edits changed the wording, so the output no longer matches the original "
            "paste word for word. That is expected - check the differences below are the "
            "ones you intended."
        )
    else:
        st.error(result.summary)
        if not result.numbers_ok:
            st.warning("Measurements / numbers missing: " + ", ".join(result.missing_numbers))

    col_a, col_b = st.columns(2)
    with col_a:
        st.caption("In the original paste, not in the .docx")
        st.write({tok: count for tok, count in result.missing[:60]} or "-")
    with col_b:
        st.caption("In the .docx, not in the original paste")
        st.write({tok: count for tok, count in result.added[:60]} or "-")
    if not user_edited:
        st.caption(
            "Common causes: the source repeated a heading word, or a line was pasted twice. "
            "Check the highlighted tokens before sending the report out."
        )


def to_pdf(docx_bytes: bytes) -> bytes | None:
    """Convert via Microsoft Word if it is installed locally. Returns None otherwise."""
    import os
    import tempfile

    try:
        from docx2pdf import convert
    except ImportError:
        return None

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "report.docx")
        dst = os.path.join(tmp, "report.pdf")
        with open(src, "wb") as fh:
            fh.write(docx_bytes)
        try:
            convert(src, dst)
        except Exception:
            return None
        if not os.path.exists(dst):
            return None
        with open(dst, "rb") as fh:
            return fh.read()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

st.title("HC Format Radiology Report Generator")
st.caption("Every word preserved, and checked. Formatting per the selected doctor's template.")

tab_single, tab_dictate, tab_batch, tab_draft, tab_templates, tab_rules = st.tabs(
    ["Report", "Dictate", "Batch", "Draft in doctor's style", "Templates", "Rules"]
)


# ---------------------------- Single report -------------------------------- #

with tab_single:
    fmt_col, src_col = st.columns([2, 3])
    with fmt_col:
        st.selectbox(
            "Report format",
            template_names,
            key="tpl_pick",
            format_func=lambda n: (
                f"{n} — {all_templates[n].doctor}" if all_templates[n].doctor else n
            ),
            help="Which doctor's house style this report comes out in. Manage them with "
                 "New / Edit / Delete in the sidebar.",
        )
        st.caption(template_summary(template))
    with src_col:
        source = st.radio("Input", ["Paste text", "Upload file"], horizontal=True)

    raw_text = ""
    if source == "Paste text":
        raw_text = st.text_area(
            "Paste the raw report exactly as the boss sent it",
            value=st.session_state.pop("prefill", ""),
            height=340,
            placeholder="MRI BRAIN WITH CONTRAST REPORT\n\nPATIENT NAME: ...\nAGE/SEX: ...\n"
            "CLINICAL HISTORY: ...\nTECHNIQUE: ...\n\nFINDINGS:\nBrain parenchyma:\n"
            "...\n\nIMPRESSION:\n...",
        )
    else:
        uploaded = st.file_uploader(
            "Report file", type=["txt", "md", "docx", "pdf", "png", "jpg", "jpeg"]
        )
        if uploaded:
            data = uploaded.getvalue()
            try:
                raw_text = readers.read_any(uploaded.name, data)
            except readers.UnreadableFile as exc:
                st.error(str(exc))
            except readers.NeedsOCR as exc:
                if use_ai and api_key and model_choice:
                    with st.spinner("Reading the scan with AI OCR..."):
                        try:
                            import ai_parser

                            raw_text = ai_parser.extract_text_from_file(
                                data, readers.mime_for(uploaded.name), api_key, model_choice
                            )
                            st.info("Text transcribed by AI - check it below before generating.")
                        except Exception as ocr_exc:
                            st.error(f"OCR failed: {ocr_exc}")
                else:
                    st.error(
                        f"{exc} Switch the engine to AI-assisted to read scanned PDFs and photos "
                        "with OCR."
                    )
            if raw_text:
                raw_text = st.text_area("Extracted text (edit if needed)", raw_text, height=300)

    if raw_text.strip():
        auto_blocks, warnings, engine_used = make_blocks(raw_text)

        for warning in warnings:
            st.warning(warning)

        # Reset the editor whenever the source text or the parsing options change,
        # so an edit is never applied to a structure that no longer exists.
        # The signature drives both the widget key and the component's nonce.
        # Reset bumps a counter so the signature changes: that mounts a brand new
        # component instance, which is the only way to make the editor drop the
        # edited DOM it is deliberately holding on to across reruns.
        reset_count = st.session_state.get("editor_resets", 0)
        signature = str(hash((raw_text, engine_used, repr(opts), template.name, reset_count)))

        head, reset = st.columns([5, 1])
        with head:
            st.subheader("Live preview — click any line and edit it")
            st.caption(f"This is the page Word will produce. Click any line to edit it. · {engine_used}")
        with reset:
            st.write("")
            if st.button("Reset", use_container_width=True, help="Discard edits and re-detect."):
                st.session_state["editor_resets"] = reset_count + 1
                st.rerun()

        blocks = structure_editor(auto_blocks, signature)

        user_edited = blocks_to_rows(blocks) != blocks_to_rows(auto_blocks)
        docx_bytes, audit_result = render_report(
            render_key(blocks, raw_text, template, letterhead, page_numbers,
                       opts.preserve_as_is, letterhead_text)
        )
        title = next((b.text for b in blocks if b.kind == "title"), "Radiology Report")

        st.divider()
        out_col, audit_col = st.columns([1, 1.4])

        with out_col:
            st.subheader("Download")
            st.caption("Edited by you" if user_edited else "Exactly as received")
            if st.session_state.get("logged_report") != (title, len(docx_bytes)):
                st.session_state["logged_report"] = (title, len(docx_bytes))
                storage.log(
                    "report.generated", title,
                    f"template={template.name}; engine={engine_used}; "
                    f"{'edited' if user_edited else 'verbatim'}",
                )
            st.download_button(
                ":material/download: Download .docx",
                data=docx_bytes,
                file_name=safe_filename(title),
                mime=DOCX_MIME,
                type="primary",
                use_container_width=True,
            )

            if st.button("Also make a PDF", use_container_width=True):
                pdf_bytes = to_pdf(docx_bytes)
                if pdf_bytes:
                    st.download_button(
                        ":material/picture_as_pdf: Download .pdf",
                        data=pdf_bytes,
                        file_name=safe_filename(title).replace(".docx", ".pdf"),
                        mime="application/pdf",
                        use_container_width=True,
                    )
                else:
                    st.info(
                        "PDF export needs Microsoft Word installed locally plus "
                        "`pip install docx2pdf`. The .docx above is unaffected."
                    )

        with audit_col:
            st.subheader("Checks")

            clinical = validate.validate(blocks)
            if clinical.ok:
                st.success("Report checks: nothing to flag.", icon=":material/check_circle:")
            else:
                critical = clinical.critical
                if critical:
                    st.error(
                        f"**{len(critical)} thing(s) to fix before sending.**",
                        icon=":material/error:",
                    )
                for f in clinical.sorted():
                    icon = {"critical": ":material/error:",
                            "warning": ":material/warning:",
                            "note": ":material/info:"}[f.severity]
                    with st.container(border=True):
                        st.markdown(f"{icon} **{f.title}**")
                        if f.detail:
                            st.caption(f.detail)
                        if f.where:
                            st.caption(f"in {f.where}")

            st.divider()
            render_audit(audit_result, user_edited=user_edited)

        if not template.builtin:
            if st.button(
                f":material/library_add: Save as a style example for {template.doctor or template.name}",
                help="Teaches the AI drafting tab this doctor's voice. Does not change how "
                     "anything is formatted.",
            ):
                learned = templates.copy_of(template, template.name)
                learned.examples = list(template.examples) + [raw_text]
                templates.save(learned)
                templates_changed()
                st.success(
                    f"Saved. {template.doctor or template.name} now has "
                    f"{len(learned.examples)} style example(s)."
                )

        with st.expander("Structure detected"):
            st.dataframe(
                [{"#": i + 1, "Format": r["Format"], "Text": r["Text"][:110], "Value": r["Value"]}
                 for i, r in enumerate(blocks_to_rows(blocks))],
                use_container_width=True,
                hide_index=True,
            )


# ------------------------------- Batch ------------------------------------- #

with tab_batch:
    st.caption("Several files, or many reports separated by a line of `---`. Returns one ZIP.")

    batch_files = st.file_uploader(
        "Report files",
        type=["txt", "md", "docx", "pdf"],
        accept_multiple_files=True,
        key="batch_files",
    )
    bulk_text = st.text_area("…or paste multiple reports, separated by a line of ---", height=200)

    jobs: list[tuple[str, str]] = []  # (label, raw_text)

    for file in batch_files or []:
        try:
            jobs.append((file.name, readers.read_any(file.name, file.getvalue())))
        except readers.NeedsOCR as exc:
            st.warning(f"{file.name}: {exc} Use the Single report tab with AI OCR.")
        except readers.UnreadableFile as exc:
            st.warning(f"{file.name}: {exc}")
        except Exception as exc:
            st.warning(f"{file.name}: could not read ({exc}).")

    if bulk_text.strip():
        chunks = [c.strip() for c in re.split(r"^\s*-{3,}\s*$", bulk_text, flags=re.M) if c.strip()]
        jobs.extend((f"pasted_{i + 1}", chunk) for i, chunk in enumerate(chunks))

    if jobs and st.button(f"Convert {len(jobs)} report(s)", type="primary"):
        buf = io.BytesIO()
        rows = []
        progress = st.progress(0.0)

        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, (label, text) in enumerate(jobs, start=1):
                blocks, _warnings, engine_used = make_blocks(text)
                docx_bytes = build_docx(blocks, template=template, letterhead=letterhead, page_numbers=page_numbers)
                title = next((b.text for b in blocks if b.kind == "title"), label)
                name = safe_filename(title)
                # Keep every file distinct even when two reports share a title.
                if name in zf.namelist():
                    name = name.replace(".docx", f"_{i}.docx")
                zf.writestr(name, docx_bytes)

                check = audit(text, docx_bytes, letterhead_text=letterhead_text, page_numbers=page_numbers,
                    preserve_as_is=opts.preserve_as_is)
                rows.append(
                    {
                        "source": label,
                        "output": name,
                        "engine": engine_used,
                        "audit": "PASS" if check.ok else "FAIL",
                        "words": check.source_tokens,
                        "detail": "" if check.ok else check.summary,
                    }
                )
                progress.progress(i / len(jobs))

        failures = sum(1 for r in rows if r["audit"] == "FAIL")
        if failures:
            st.error(f"{failures} of {len(rows)} report(s) failed the word-loss audit - see the table.")
        else:
            st.success(f"All {len(rows)} report(s) converted, audit passed.")

        st.dataframe(rows, use_container_width=True, hide_index=True)
        st.download_button(
            ":material/folder_zip: Download all as ZIP",
            data=buf.getvalue(),
            file_name=f"HC_Format_Reports_{datetime.now():%Y%m%d_%H%M}.zip",
            mime="application/zip",
            type="primary",
        )


# ------------------------------- Rules ------------------------------------- #

# ------------------------------- Dictate ------------------------------------ #
#
# Speak the report, get accurate text. The loop that matters:
#
#   record -> transcribe -> anything unheard comes back as a question
#          -> doctor picks, types, or RE-RECORDS just that phrase
#          -> correction teaches the vocabulary, so it hears it next time
#
# Nothing unclear is ever silently guessed. Uncertain words are bracketed in the
# transcript and listed, so a wrong word cannot slip into a report unseen.

with tab_dictate:
    st.subheader("Dictate the report")

    if not use_ai:
        st.info("Switch the sidebar engine to **AI-assisted** to dictate.")
    elif not api_key or not model_choice:
        st.info(
            "No Gemini key is configured, so dictation is off. Put your key in "
            "`.streamlit/secrets.toml` as `GEMINI_API_KEY` and reload — see "
            "`secrets.toml.example`."
        )
    else:
        doctor_label = template.doctor or template.name

        top, clear_col = st.columns([5, 1])
        with top:
            st.caption(
                f"Listening for **{doctor_label}** · {len(template.vocabulary)} known term(s), "
                f"{len(template.dictation_fixes)} past mishearing(s) · model `{model_choice}`"
            )
        with clear_col:
            if st.button("Clear", use_container_width=True, key="dict_clear"):
                for key in list(st.session_state):
                    if key.startswith("dict_"):
                        st.session_state.pop(key, None)
                st.rerun()

        if template.builtin:
            st.info(
                "You are on the built-in **HC FORMAT**, which cannot learn a voice. Create a "
                "template for this doctor with **➕ New** in the sidebar — then every word you "
                "correct here is remembered."
            )

        engine_key = "gemini"
        ai4b_model = ""
        ai4b_language = "hi"
        ai4b_local = False
        ai4b_remote_code = False

        with st.expander("Speech engine", expanded=False):
            if speech is None:
                st.warning(
                    "The AI4Bharat speech options could not load, so dictation is running on "
                    f"Gemini only. Underlying error: `{SPEECH_IMPORT_ERROR}`"
                )
            else:
                engine_key = st.radio(
                    "Who does the listening?",
                    list(speech.ENGINES),
                    format_func=lambda k: speech.ENGINES[k],
                    key="dict_engine",
                    help="AI4Bharat is IIT Madras's Indic speech recognition, trained on Indian "
                         "speech across 22 languages. It hears Indian accents and Hindi/English "
                         "mixing far better than a general model, but returns bare words — so "
                         "pairing it with Gemini for the layout is usually the best setting.",
                )

            if speech is not None and engine_key.startswith("ai4bharat"):
                c1, c2 = st.columns(2)
                with c1:
                    ai4b_language = st.selectbox(
                        "Language spoken",
                        list(speech.LANGUAGES),
                        format_func=lambda c: speech.LANGUAGES[c],
                        index=list(speech.LANGUAGES).index(
                            configured_secret("AI4BHARAT_LANGUAGE", "hi")
                            if configured_secret("AI4BHARAT_LANGUAGE", "hi") in speech.LANGUAGES
                            else "hi"
                        ),
                        key="dict_lang",
                    )
                with c2:
                    preset = st.selectbox(
                        "Model",
                        speech.AI4BHARAT_PRESETS + ["Custom / paste a repo ID"],
                        key="dict_ai4b_preset",
                    )
                ai4b_model = (
                    st.text_input(
                        "Hugging Face repo ID",
                        value=configured_secret("AI4BHARAT_MODEL", speech.AI4BHARAT_PRESETS[0]),
                        key="dict_ai4b_custom",
                    ).strip()
                    if preset.startswith("Custom")
                    else preset
                )
                ai4b_local = st.checkbox(
                    "Run the model on this machine (audio never leaves the building)",
                    value=str(configured_secret("AI4BHARAT_LOCAL", "")).lower()
                    in ("1", "true", "yes"),
                    key="dict_ai4b_local",
                    help="Needs `pip install torch transformers` and a one-time model download "
                         "of a gigabyte or so. After that it works with no internet — the right "
                         "choice for identifiable patient dictation.",
                )
                ai4b_remote_code = False
                if ai4b_local:
                    st.caption(
                        "Local mode: the first dictation downloads the model and will be slow. "
                        "Later ones are fast and fully offline."
                    )
                    ai4b_remote_code = st.checkbox(
                        "Allow this model to run its own code",
                        value=False,
                        key="dict_ai4b_code",
                        help="Some models ship custom Python that runs while loading. Leave "
                             "this OFF unless the model card says it is required AND you trust "
                             "the repository — it executes code on this machine.",
                    )
                    if ai4b_remote_code:
                        st.warning(
                            f"`{ai4b_model}` will be allowed to execute its own Python here. "
                            "Only do this for a repository you trust."
                        )
                elif not configured_secret("HF_TOKEN"):
                    st.warning(
                        "Hosted AI4Bharat needs a free Hugging Face token. Add `HF_TOKEN` to "
                        "`.streamlit/secrets.toml`, or tick *Run on this machine* instead."
                    )
                st.caption(
                    "AI4Bharat publish and rename repositories often — if a model ID 404s, "
                    "check the current one on huggingface.co and paste it above."
                )

        context = st.text_input(
            "What is this study? (optional — helps accuracy a lot)",
            key="dict_context",
            placeholder="MRI brain with contrast, post-op pituitary macroadenoma",
        )

        st.caption("Speak normally — punctuation, headings and spoken corrections are handled.")

        # Live view while speaking, then an accurate pass over the same audio.
        session = _live_dictate(key="dict_live", default=None)

        if session and session.get("nonce") != st.session_state.get("dict_nonce"):
            st.session_state["dict_nonce"] = session.get("nonce")
            audio_b64 = session.get("audio") or ""
            if not audio_b64:
                st.warning("No audio came through. Check the microphone and try again.")
            else:
                import base64

                import ai_parser

                raw_audio = base64.b64decode(audio_b64)
                mime = session.get("mime") or "audio/webm"
                study = st.session_state.get("dict_context", "")
                result = None

                try:
                    if engine_key == "gemini":
                        with st.spinner("Transcribing what you said..."):
                            result = ai_parser.transcribe_dictation(
                                raw_audio, mime, template, api_key, model_choice, context=study
                            )
                    else:
                        with st.spinner(f"Listening with AI4Bharat ({ai4b_model})..."):
                            heard = speech.transcribe_ai4bharat(
                                raw_audio, mime,
                                model=ai4b_model,
                                language=ai4b_language,
                                hf_token=configured_secret("HF_TOKEN"),
                                run_locally=ai4b_local,
                                allow_remote_code=ai4b_remote_code,
                            )
                        st.session_state["dict_asr_text"] = heard.text
                        st.caption(heard.note)

                        if engine_key == "ai4bharat+gemini":
                            with st.spinner("Laying it out as a report..."):
                                result = ai_parser.structure_dictation(
                                    heard.text, template, api_key, model_choice,
                                    context=study,
                                    language=speech.LANGUAGES.get(ai4b_language, ""),
                                )
                        else:
                            # Raw ASR only: no layout pass, so nothing is flagged either.
                            result = {
                                "transcript": heard.text,
                                "unclear": [],
                                "audio_quality": "good",
                                "notes": "Raw AI4Bharat output — no punctuation or layout was "
                                         "applied, and nothing was checked for uncertainty.",
                            }

                    # Deterministic cleanup after the model: spoken numbers to
                    # figures, units normalised. Costs nothing and cannot invent.
                    cleaned = dictation_fix.clean(result["transcript"], template.vocabulary)
                    result["transcript"] = cleaned.text
                    suggestions = [
                        {"heard": x.heard, "suggested": x.suggested,
                         "confidence": x.confidence, "why": f"{x.reason} match"}
                        for x in (cleaned.suggestions or [])
                    ]
                    # Rules catch split words and near-miss spellings. Meaning
                    # catches the rest - "colic list" for "cholelithiasis" is
                    # too far away for any letter or sound comparison.
                    already = {s["heard"] for s in suggestions}
                    try:
                        for extra in ai_parser.review_transcript(
                            cleaned.text, template, api_key, model_choice
                        ):
                            if extra["heard"] not in already:
                                suggestions.append({**extra, "confidence": 0.0})
                    except Exception:
                        pass  # a proofread that fails must not lose the transcript

                    st.session_state["dict_cleanup"] = {
                        "note": cleaned.note,
                        "suggestions": suggestions,
                    }

                    st.session_state["dict_result"] = result
                    st.session_state["dict_text"] = result["transcript"]
                    st.session_state["dict_original"] = result["transcript"]
                    st.session_state["dict_live_text"] = session.get("text", "")
                    st.rerun()
                except SpeechError as exc:
                    st.error(str(exc))
                except Exception as exc:
                    st.error(f"Transcription failed: {exc}")

        if st.session_state.get("dict_result"):
            rough = {
                "Browser live preview": st.session_state.get("dict_live_text", ""),
                "AI4Bharat raw output": st.session_state.get("dict_asr_text", ""),
            }
            rough = {k: v for k, v in rough.items() if v}
            if rough:
                with st.expander("What each stage heard (before the layout pass)"):
                    for label, value in rough.items():
                        st.markdown(f"**{label}**")
                        st.write(value)

        result = st.session_state.get("dict_result")
        if result:
            quality = result.get("audio_quality", "")
            if quality == "very poor":
                st.error(
                    "The recording was very poor. Check the microphone and the room, then "
                    "record again — a bad recording is not worth correcting word by word."
                )
            elif quality == "noisy":
                st.warning("The recording was noisy. Check the flagged words carefully.")
            if result.get("notes"):
                st.caption(result["notes"])

            # ---------- ask about anything not heard ---------- #

            unclear = result.get("unclear", [])
            if unclear:
                st.subheader(f"{len(unclear)} thing(s) I did not hear clearly")
                st.caption(
                    "These are bracketed in the transcript below. Confirm each one — pick an "
                    "option, type it, or say it again."
                )

                for item in unclear:
                    with st.container(border=True):
                        st.markdown(f"**Heard:** `{item['heard']}`")
                        if item.get("reason"):
                            st.caption(item["reason"])

                        options = item["options"] + ["Type it", "Say it again"]
                        choice = st.radio(
                            "What did you say?",
                            options,
                            key=f"dict_fix_opt_{item['id']}",
                            horizontal=len(options) <= 4,
                            label_visibility="collapsed",
                        )

                        settled = ""
                        if choice == "Type it":
                            settled = st.text_input(
                                "Type the words", key=f"dict_fix_txt_{item['id']}"
                            )
                        elif choice == "Say it again":
                            st.caption("Record just this phrase — a few words is enough.")
                            again = st.audio_input(
                                "Repeat it", key=f"dict_fix_audio_{item['id']}"
                            )
                            if again is not None and st.button(
                                "Use this recording", key=f"dict_fix_go_{item['id']}"
                            ):
                                import ai_parser

                                with st.spinner("Listening again..."):
                                    try:
                                        heard_again = ai_parser.transcribe_repeat(
                                            again.getvalue(), again.type or "audio/wav",
                                            template, api_key, model_choice,
                                            heard=item["heard"], reason=item.get("reason", ""),
                                        )
                                    except Exception as exc:
                                        st.error(f"Could not transcribe the repeat: {exc}")
                                        heard_again = ""
                                if heard_again == "UNCLEAR":
                                    st.warning(
                                        "Still not clear. Try once more, closer to the "
                                        "microphone, or type it instead."
                                    )
                                elif heard_again:
                                    st.success(f"Heard: **{heard_again}**")
                                    st.session_state[f"dict_fix_val_{item['id']}"] = heard_again
                            settled = st.session_state.get(f"dict_fix_val_{item['id']}", "")
                        else:
                            settled = choice

                        if settled and st.button(
                            f"Apply “{settled[:40]}”", key=f"dict_apply_{item['id']}"
                        ):
                            text = st.session_state.get("dict_text", "")
                            # Replace the bracketed guess, or the bare text if the
                            # model did not bracket it.
                            for target in (f"[[{item['heard']}]]", item["heard"]):
                                if target in text:
                                    text = text.replace(target, settled, 1)
                                    break
                            st.session_state["dict_text"] = text
                            st.session_state.setdefault("dict_fixes", []).append(
                                (item["heard"], settled)
                            )
                            st.session_state["dict_result"]["unclear"] = [
                                u for u in unclear if u["id"] != item["id"]
                            ]
                            st.rerun()

            # ---------- the transcript ---------- #

            st.divider()
            cleanup = st.session_state.get("dict_cleanup") or {}
            if cleanup.get("note"):
                st.caption(f"Tidied automatically: {cleanup['note']}.")
            for hit in cleanup.get("suggestions", [])[:8]:
                why = hit.get("why") or "similar to a term you use"
                if hit.get("confidence"):
                    why += f" · {int(hit['confidence'] * 100)}%"
                st.info(
                    f"Heard **{hit['heard']}** — did you mean **{hit['suggested']}**?\n\n"
                    f"_{why}_",
                    icon=":material/spellcheck:",
                )

            st.subheader("Transcript — correct anything that is wrong")
            transcript = st.text_area(
                "Transcript",
                value=st.session_state.get("dict_text", ""),
                height=340,
                key="dict_edit",
                label_visibility="collapsed",
            )

            remaining = transcript.count("[[")
            if remaining:
                st.warning(
                    f"{remaining} uncertain word(s) still bracketed as `[[...]]`. Settle them "
                    "above, or edit them out by hand — do not send a report with brackets in it."
                )
            else:
                st.success("Nothing uncertain left in the transcript.")

            # ---------- teach the voice ---------- #

            original = st.session_state.get("dict_original", "")
            pending = st.session_state.get("dict_fixes", [])
            changed = transcript.strip() != original.strip()

            st.divider()
            st.subheader("Teach this doctor's voice")
            if template.builtin:
                st.caption(
                    "The built-in HC FORMAT cannot learn. Create a template for this doctor "
                    "with **➕ New** in the sidebar."
                )
            elif not changed and not pending:
                st.caption(
                    "Nothing was corrected, so there is nothing to learn. Fix a misheard word "
                    "and this turns on."
                )
            else:
                if pending:
                    st.caption(
                        "Confirmed while you were fixing: "
                        + " · ".join(f"`{h}` → **{s}**" for h, s in pending[:6])
                    )
                if st.button(":material/school: Learn these words", type="primary", key="dict_learn"):
                    import ai_parser

                    learned = template
                    for heard, said in pending:
                        learned = templates.remember_dictation_fix(learned, heard, said)

                    terms: list[str] = []
                    if changed:
                        with st.spinner("Working out which terms to remember..."):
                            try:
                                terms = ai_parser.distill_vocabulary(
                                    original, transcript, template, api_key, model_choice
                                )
                            except Exception as exc:
                                st.warning(
                                    f"Could not pull out terms ({exc}). The word-level fixes "
                                    "were still saved."
                                )
                        learned = templates.remember_vocabulary(learned, terms)

                    templates.save(learned)

                    templates_changed()
                    st.session_state["dict_fixes"] = []
                    st.session_state["dict_original"] = transcript
                    st.success(
                        f"Learned. {doctor_label} now knows "
                        f"{templates.learning_summary(learned)}."
                    )
                    if terms:
                        st.caption("Terms remembered: " + ", ".join(terms[:20]))
                    st.rerun()

            # ---------- what next ---------- #

            st.divider()
            st.subheader("Take it forward")

            go_draft, go_report = st.columns(2)
            with go_draft:
                if st.button(":material/edit_note: Send to drafting", type="primary",
                             use_container_width=True, key="dict_to_draft",
                             help="Rewrites it into this doctor's house style. Use for rough "
                                  "notes. You pick the report format afterwards."):
                    st.session_state["draft_in"] = transcript
                    st.success("Loaded — open **Draft in doctor's style**.")
            with go_report:
                if st.button(":material/arrow_forward: Send to report", use_container_width=True,
                             key="dict_to_single",
                             help="Straight to formatting, word for word. Use when you dictated "
                                  "the finished report."):
                    st.session_state["prefill"] = transcript
                    st.success("Loaded — open **Report** and choose the format.")

            with st.expander("Raw text — copy it out"):
                st.caption("Hover the block and use the copy icon in its top-right corner.")
                st.code(transcript, language=None, wrap_lines=True)
                st.download_button(
                    ":material/download: Download as .txt",
                    data=transcript.encode("utf-8"),
                    file_name="dictation.txt",
                    mime="text/plain",
                    key="dict_dl",
                )


# ------------------------ Draft in doctor's style --------------------------- #
#
# A four-step loop that gets better at one doctor over time:
#   1. notes in
#   2. the AI asks about anything genuinely ambiguous, with options
#   3. the doctor edits the draft
#   4. the edit is fed back into the doctor's template as reusable rules
#
# Step 4 is the point. Everything the doctor changes teaches the next report.

with tab_draft:
    st.warning(
        "**This tool rewrites words — everywhere else in the app they are preserved verbatim.** "
        "For a doctor drafting their own report, never for one someone else sent you. "
        "Read every line before signing.",
        icon=":material/warning:",
    )

    if not use_ai:
        st.info("Switch the sidebar engine to **AI-assisted** to use this.")
    elif not api_key or not model_choice:
        st.info(
            "No Gemini key is configured, so this tab is off. Put your key in "
            "`.streamlit/secrets.toml` as `GEMINI_API_KEY` and reload — see "
            "`secrets.toml.example`."
        )
    else:
        doctor_label = template.doctor or template.name

        head, reset_draft = st.columns([5, 1])
        with head:
            st.caption(
                f"Writing as **{doctor_label}** · learned so far: "
                f"{templates.learning_summary(template)} · model `{model_choice}`"
            )
        with reset_draft:
            if st.button("Start over", use_container_width=True):
                for key in ("draft_out", "draft_in", "draft_questions", "draft_assumptions",
                            "draft_answers", "draft_original", "draft_section"):
                    st.session_state.pop(key, None)
                st.rerun()

        if template.builtin:
            st.info(
                "You are on the built-in **HC FORMAT**, which holds no doctor's voice and cannot "
                "learn. Create a template for this doctor with **➕ New** in the sidebar — then "
                "every correction here is remembered."
            )
        elif not (template.examples or template.preferences or template.corrections):
            st.info(
                f"Nothing is on file for {doctor_label} yet, so this first draft will stay close "
                "to your notes. Correct it and press *Teach* below — from the second report on, "
                "it starts sounding like them."
            )

        # ---------------- Step 1: notes ---------------- #

        section = st.selectbox(
            "Rewrite",
            ["the IMPRESSION only", "the FINDINGS only", "the whole report"],
            help="Narrower is safer. The IMPRESSION is where house style shows most.",
        )
        rough = st.text_area(
            "Rough notes — shorthand is fine",
            value=st.session_state.get("draft_in", ""),
            height=200,
            placeholder="mult gallstones 4-11mm, no chole\nliver 14.2cm normal, no SOL\nkidneys nad",
        )

        def run_draft(notes: str, answers: dict) -> None:
            import ai_parser

            with st.spinner("Drafting..."):
                try:
                    result = ai_parser.draft_with_questions(
                        notes, template, api_key, model_choice,
                        section=section, answers=answers,
                    )
                except Exception as exc:
                    st.error(f"Drafting failed: {exc}")
                    return
            st.session_state["draft_in"] = notes
            st.session_state["draft_section"] = section
            st.session_state["draft_out"] = result["draft"]
            st.session_state["draft_original"] = result["draft"]  # the untouched AI version
            st.session_state["draft_questions"] = result["questions"]
            st.session_state["draft_assumptions"] = result["assumptions"]

        if st.button(":material/edit_note: Write in this doctor's style", type="primary", disabled=not rough.strip()):
            run_draft(rough, st.session_state.get("draft_answers", {}))
            st.rerun()

        # ---------------- Step 2: the AI asks ---------------- #

        questions = st.session_state.get("draft_questions", [])
        if questions:
            st.subheader("A few things I am not sure about")
            st.caption(
                "Answer what matters and redraft. Anything you skip stays as the assumption "
                "listed below. Answers you save are never asked again."
            )
            answers: dict[str, str] = {}
            for q in questions:
                options = list(q["options"]) + ["Let me type it"]
                choice = st.radio(
                    q["question"],
                    options,
                    key=f"q_opt_{q['id']}",
                    horizontal=len(options) <= 3,
                    help=q.get("why") or None,
                )
                if choice == "Let me type it":
                    choice = st.text_input(
                        "Your answer", key=f"q_txt_{q['id']}", placeholder="Type it as it should read"
                    )
                if choice:
                    answers[q["question"]] = choice

            ans_a, ans_b = st.columns([2, 1])
            with ans_a:
                if st.button(":material/refresh: Redraft with these answers", type="primary", use_container_width=True):
                    merged = {**st.session_state.get("draft_answers", {}), **answers}
                    st.session_state["draft_answers"] = merged
                    run_draft(st.session_state.get("draft_in", rough), merged)
                    st.rerun()
            with ans_b:
                if st.button("Remember these answers", use_container_width=True,
                             disabled=template.builtin or not answers,
                             help="Save them on this doctor's template so the question never "
                                  "comes up again."):
                    learned = template
                    for question, answer in answers.items():
                        learned = templates.remember_answer(learned, question, answer)
                    templates.save(learned)
                    templates_changed()
                    st.success(f"Saved {len(answers)} answer(s) to {doctor_label}.")
                    st.rerun()

        for assumption in st.session_state.get("draft_assumptions", []):
            st.caption(f"Assumed: {assumption}")

        # ---------------- Step 3: the doctor edits ---------------- #

        drafted = st.session_state.get("draft_out", "")
        if drafted:
            st.divider()
            left, right = st.columns(2)
            with left:
                st.markdown("**Your notes**")
                st.code(st.session_state.get("draft_in", ""), language=None)
            with right:
                st.markdown("**Draft — correct anything that is not how you write**")
                drafted = st.text_area("Edit the draft", drafted, height=340, key="draft_edit")

            try:
                import ai_parser

                dropped = ai_parser.missing_facts(st.session_state.get("draft_in", ""), drafted)
            except Exception:
                dropped = []

            if dropped:
                st.error(
                    "These numbers from your notes are **not** in the draft: "
                    + ", ".join(dropped)
                    + ". Check nothing was lost or altered before using it."
                )
            else:
                st.success("Every number from your notes appears in the draft.")
                st.caption(
                    "This checks measurements only. It cannot tell you whether a finding was "
                    "reworded — that is your read."
                )

            # ---------------- Step 4: teach ---------------- #

            original = st.session_state.get("draft_original", "")
            corrected = drafted.strip() != original.strip()

            st.divider()
            st.subheader("Teach this doctor's style")
            if template.builtin:
                st.caption(
                    "The built-in HC FORMAT cannot learn. Create a template for this doctor "
                    "with **➕ New** in the sidebar."
                )
            elif not corrected:
                st.caption(
                    "You have not changed the draft, so there is nothing to learn from yet. "
                    "Correct anything that is not how you write, then this button turns on."
                )
            else:
                note = st.text_input(
                    "Why did you change it? (optional — one line makes the lesson much sharper)",
                    key="draft_note",
                    placeholder="I always write calculi, not stones, and I number the impression.",
                )
                if st.button(":material/school: Learn from my corrections", type="primary"):
                    import ai_parser

                    rules: list[str] = []
                    with st.spinner("Working out what you want in general..."):
                        try:
                            rules = ai_parser.distill_preferences(
                                original, drafted, template, api_key, model_choice, note=note
                            )
                        except Exception as exc:
                            st.warning(
                                f"Could not distil a general rule ({exc}). Saving the correction "
                                "itself, which the next draft still learns from."
                            )
                    learned = templates.remember_correction(
                        template, original, drafted, note=note, rules=rules
                    )
                    templates.save(learned)
                    templates_changed()
                    st.session_state["draft_original"] = drafted  # this edit is now taught
                    if rules:
                        st.success("Learned:")
                        for rule in rules:
                            st.markdown(f"- {rule}")
                    else:
                        st.success(
                            "Correction saved. No general rule came out of this one, but the "
                            "before/after pair goes into the next draft."
                        )
                    st.rerun()

            # ---------------- Finish ---------------- #

            st.divider()
            st.subheader("Report finished — what should happen to it?")


            opt_a, opt_b = st.columns(2)

            with opt_a:
                st.markdown(f"**1 · Save to {doctor_label}**")
                st.caption(
                    "Adds this report to their voice samples and keeps every rule learned above. "
                    "Use this for a doctor who already has a template."
                )
                if st.button(f":material/library_add: Save to {doctor_label}", use_container_width=True,
                             disabled=template.builtin, type="primary"):
                    learned = templates.remember_example(template, drafted)
                    if corrected:
                        learned = templates.remember_correction(learned, original, drafted)
                    templates.save(learned)
                    templates_changed()
                    st.success(
                        f"Saved. {doctor_label} now knows "
                        f"{templates.learning_summary(learned)}."
                    )
                    st.rerun()

            with opt_b:
                st.markdown("**2 · Set up a new doctor from this report**")
                st.caption(
                    "Creates a fresh template — formatting, letterhead and voice — seeded with "
                    "this report. Use this the first time a doctor's work comes through."
                )
                if st.button(":material/person_add: Create a doctor from this report", use_container_width=True):
                    _dialog_seed_template(drafted, (original, drafted) if corrected else None)

            st.download_button(
                ":material/download: Download draft",
                data=drafted.encode("utf-8"),
                file_name="draft.txt",
                mime="text/plain",
            )
            if st.button(":material/arrow_forward: Send to report", type="primary",
                          use_container_width=True,
                          help="Choose the doctor's format there; from that point the text is "
                               "treated verbatim and the audit applies again."):
                st.session_state["prefill"] = drafted
                st.success("Loaded — open **Report** and choose the format.")



# --------------------------- Doctor templates ------------------------------ #

with tab_templates:
    st.caption("One doctor's house style. Pick it in the sidebar; reports follow it automatically.")

    editing_name = st.selectbox(
        "Edit template", template_names, index=template_names.index(picked_template),
        key="tpl_edit_pick",
    )
    editing = all_templates[editing_name]

    if editing.builtin:
        st.info(
            "**HC FORMAT (default)** is the signed-off format and is read-only. "
            "Use *Save as new* below to start a doctor's template from it."
        )

    meta_a, meta_b = st.columns(2)
    with meta_a:
        t_name = st.text_input("Template name", value=editing.name, key="tpl_name")
        t_doctor = st.text_input("Doctor", value=editing.doctor, key="tpl_doctor",
                                 placeholder="Dr. Sharad Kulkarni")
        t_font = st.text_input("Font", value=editing.font_name, key="tpl_font")
        t_size = st.number_input("Font size (pt)", 6.0, 36.0, float(editing.font_size), 0.5,
                                 key="tpl_size")
        t_colour = st.text_input("Font colour (hex)", value=editing.font_color, key="tpl_colour",
                                 help="000000 is black. HC FORMAT rule 1 is black only.")
    with meta_b:
        t_spacing = st.number_input("Line spacing", 1.0, 3.0, float(editing.line_spacing), 0.05,
                                    key="tpl_spacing")
        m1, m2 = st.columns(2)
        with m1:
            t_top = st.number_input("Margin top (in)", 0.2, 3.0, float(editing.margin_top), 0.1,
                                    key="tpl_top")
            t_left = st.number_input("Margin left (in)", 0.2, 3.0, float(editing.margin_left), 0.1,
                                     key="tpl_left")
        with m2:
            t_bottom = st.number_input("Margin bottom (in)", 0.2, 3.0, float(editing.margin_bottom),
                                       0.1, key="tpl_bottom")
            t_right = st.number_input("Margin right (in)", 0.2, 3.0, float(editing.margin_right),
                                      0.1, key="tpl_right")
        t_pages = st.checkbox("Page numbers by default", value=editing.page_numbers,
                              key="tpl_pages")

    st.markdown("**Letterhead carried by this template**")
    l1, l2, l3 = st.columns(3)
    with l1:
        t_lh_name = st.text_input("Clinic name ", value=editing.letterhead_name, key="tpl_lh_name")
    with l2:
        t_lh_addr = st.text_input("Address ", value=editing.letterhead_address, key="tpl_lh_addr")
    with l3:
        t_lh_contact = st.text_input("Phone / email ", value=editing.letterhead_contact,
                                     key="tpl_lh_contact")

    st.markdown("**How each kind of line looks**")
    style_rows = [
        {
            "Line": kind,
            "Bold": editing.style(kind).bold,
            "Italic": editing.style(kind).italic,
            "Underline": editing.style(kind).underline,
            "UPPERCASE": editing.style(kind).uppercase,
            "Bullet": editing.style(kind).bullet,
            "Align": editing.style(kind).align,
            "Space before": float(editing.style(kind).space_before),
            "Space after": float(editing.style(kind).space_after),
        }
        for kind in templates.BLOCK_KINDS
    ]
    edited_styles = st.data_editor(
        style_rows,
        key=f"tpl_styles::{editing_name}",
        hide_index=True,
        use_container_width=True,
        column_config={
            "Line": st.column_config.TextColumn("Line", disabled=True),
            "Align": st.column_config.SelectboxColumn("Align", options=list(templates.ALIGNMENTS)),
            "Space before": st.column_config.NumberColumn("Space before (pt)", min_value=0.0,
                                                          max_value=48.0, step=1.0),
            "Space after": st.column_config.NumberColumn("Space after (pt)", min_value=0.0,
                                                         max_value=48.0, step=1.0),
        },
    )

    def collect_template(name: str) -> templates.Template:
        rows = edited_styles.to_dict("records") if hasattr(edited_styles, "to_dict") else edited_styles
        styles = {}
        for row in rows:
            styles[str(row["Line"])] = templates.BlockStyle(
                bold=bool(row["Bold"]),
                italic=bool(row["Italic"]),
                underline=bool(row["Underline"]),
                uppercase=bool(row["UPPERCASE"]),
                bullet=bool(row["Bullet"]),
                align=str(row["Align"]),
                space_before=float(row["Space before"]),
                space_after=float(row["Space after"]),
            )
        return templates.Template(
            name=name, doctor=t_doctor, font_name=t_font, font_size=float(t_size),
            font_color=t_colour, line_spacing=float(t_spacing),
            margin_top=float(t_top), margin_bottom=float(t_bottom),
            margin_left=float(t_left), margin_right=float(t_right),
            page_numbers=bool(t_pages), letterhead_name=t_lh_name,
            letterhead_address=t_lh_addr, letterhead_contact=t_lh_contact,
            styles=styles,
            examples=[e for e in kept_examples if e.strip()],
            style_notes=t_style_notes,
            corrections=list(editing.corrections),
            preferences=kept_preferences,
            answered=kept_answers,
            vocabulary=kept_vocabulary,
            dictation_fixes=list(editing.dictation_fixes),
        )

    st.markdown("**Learned from corrections** — rules the AI worked out from this doctor's edits")
    st.caption(
        "These bind harder than the examples. Untick one to make the AI forget it. "
        f"Currently: {templates.learning_summary(editing)}."
    )
    kept_preferences: list[str] = []
    if editing.preferences:
        for i, rule in enumerate(editing.preferences):
            if st.checkbox(rule, value=True, key=f"tpl_pref_{i}"):
                kept_preferences.append(rule)
    else:
        st.caption(
            "Nothing yet. Correct a draft in the *Draft in doctor's style* tab and press "
            "**🎓 Learn from my corrections**."
        )

    if editing.corrections:
        with st.expander(f"Correction history ({len(editing.corrections)})"):
            for c in reversed(editing.corrections):
                st.caption(f"{c.when}" + (f" — {c.note}" if c.note else ""))
                d1, d2 = st.columns(2)
                with d1:
                    st.markdown("*Draft*")
                    st.code(c.before[:800], language=None)
                with d2:
                    st.markdown("*Corrected*")
                    st.code(c.after[:800], language=None)
                st.divider()

    kept_answers = dict(editing.answered)
    if editing.answered:
        with st.expander(f"Questions already answered ({len(editing.answered)})"):
            st.caption("The AI will not ask these again. Untick to let it ask once more.")
            for i, (question, answer) in enumerate(list(editing.answered.items())):
                if not st.checkbox(f"**{question}** → {answer}", value=True, key=f"tpl_ans_{i}"):
                    kept_answers.pop(question, None)

    kept_vocabulary = list(editing.vocabulary)
    if editing.vocabulary or editing.dictation_fixes:
        with st.expander(
            f"Dictation vocabulary ({len(editing.vocabulary)} term(s), "
            f"{len(editing.dictation_fixes)} mishearing(s) fixed)"
        ):
            st.caption(
                "Learned in the **🎙 Dictate** tab. These bias transcription towards the words "
                "this doctor actually says. Untick a term to drop it."
            )
            if editing.dictation_fixes:
                st.markdown("**Words that were misheard before**")
                for c in reversed(editing.dictation_fixes[-15:]):
                    st.caption(f"heard `{c.before}` → said **{c.after}**")
            if editing.vocabulary:
                st.markdown("**Known terms**")
                cols = st.columns(3)
                kept_vocabulary = []
                for i, term in enumerate(editing.vocabulary):
                    with cols[i % 3]:
                        if st.checkbox(term, value=True, key=f"tpl_vocab_{i}"):
                            kept_vocabulary.append(term)

    st.markdown("**Style examples** — past reports the AI learns this doctor's voice from")
    if editing.builtin:
        st.caption("The built-in format holds no examples. Create a doctor's template to add some.")
    st.caption(f"{len(editing.examples)} on file · two or three is usually enough · drafting only")
    t_style_notes = st.text_area(
        "House-style notes (optional, free text)",
        value=editing.style_notes,
        key="tpl_notes",
        height=70,
        placeholder="Always number the impression. Never use the word 'unremarkable'. "
                    "Close with 'Please correlate clinically.'",
    )

    kept_examples: list[str] = []
    for i, example in enumerate(editing.examples):
        with st.expander(f"Example {i + 1} — {example.strip().splitlines()[0][:60]}"):
            text = st.text_area("Report text", value=example, height=200, key=f"tpl_ex_{i}")
            if st.checkbox("Remove this example", key=f"tpl_ex_del_{i}"):
                st.caption("Will be dropped on Save.")
            else:
                kept_examples.append(text)

    new_example = st.text_area(
        "Add a past report", height=140, key="tpl_ex_new",
        placeholder="Paste a finished report this doctor already signed.",
    )
    if new_example.strip():
        kept_examples.append(new_example)

    # ---- Unsaved-change detection -------------------------------------- #
    # Streamlit widgets hold their values in the browser, so an edit that is
    # never saved is silently lost on the next reload. Compare what is on screen
    # with what is on disk and say so plainly.
    pending = collect_template(t_name)
    saved_state = templates.to_dict(editing)
    pending_state = templates.to_dict(pending)
    for state in (saved_state, pending_state):
        state.pop("builtin", None)
    dirty = pending_state != saved_state or t_name.strip() != editing.name

    st.divider()
    if dirty and not editing.builtin:
        changed = sorted(
            k for k in pending_state
            if pending_state.get(k) != saved_state.get(k)
        )
        st.warning(
            "**Unsaved changes** — " + ", ".join(changed[:6])
            + ("…" if len(changed) > 6 else "")
            + ". They are lost if you reload without saving.",
            icon=":material/edit_note:",
        )
    elif dirty and editing.builtin:
        st.info(
            "HC FORMAT is read-only. Use **Save as new** to keep these changes as a "
            "doctor's own template.",
            icon=":material/lock:",
        )
    else:
        st.caption("No unsaved changes.")

    act1, act2, act3 = st.columns(3)
    with act1:
        if st.button(":material/save: Save" + (" •" if dirty else ""),
                     use_container_width=True, type="primary" if dirty else "secondary",
                     disabled=editing.builtin,
                     help="The built-in HC FORMAT cannot be overwritten."
                          if editing.builtin else "Write these changes to disk."):
            try:
                templates.save(pending, expect=st.session_state.get(f"fp::{editing.name}"))
                templates_changed()
            except templates.ConflictError as exc:
                st.error(str(exc))
            else:
                st.success(f"Saved “{t_name}”.")
                st.rerun()
    with act2:
        if st.button(":material/content_copy: Save as new", use_container_width=True,
                     type="primary" if (dirty and editing.builtin) else "secondary",
                     help="Keep the original and create a second template from these settings."):
            new_name = t_name if t_name != editing.name else f"{t_name} copy"
            if new_name in all_templates and new_name != t_name:
                st.error(f"“{new_name}” already exists — change the name first.")
            else:
                templates.save(templates.copy_of(pending, new_name))
                templates_changed()
                st.success(f"Created “{new_name}”. Pick it as the report format.")
                st.rerun()
    with act3:
        if st.button(":material/delete: Delete", use_container_width=True,
                     disabled=editing.builtin):
            _dialog_delete(editing)

    # Remember what the file looked like when this form was drawn, so a save can
    # tell whether someone else changed it in the meantime.
    st.session_state[f"fp::{editing.name}"] = templates.fingerprint(editing.name)

    st.divider()
    with st.expander("Activity log"):
        store = storage.get_store()
        st.caption(f"Storage: {store.describe()}")
        rows = store.events(limit=200)
        if not rows:
            st.caption(
                "Nothing recorded yet. Reports generated and templates saved are logged from "
                "now on."
            )
        else:
            st.dataframe(
                [
                    {
                        "When": e.when.replace("T", " ")[:16],
                        "What": e.kind,
                        "Subject": e.subject,
                        "Detail": e.detail,
                        "Who": e.user or "—",
                    }
                    for e in rows
                ],
                use_container_width=True,
                hide_index=True,
            )
            if not any(e.user for e in rows):
                st.caption(
                    "No names against these rows because no identity provider is configured. "
                    "Set up `[auth]` in secrets.toml and Streamlit fills in who did what."
                )

    with st.expander("Preview this template on a sample report"):
        sample = parse_report(
            "MRI BRAIN WITH CONTRAST REPORT\n"
            "PATIENT NAME: Mr. Sample Patient\n"
            "CLINICAL HISTORY: Headache since 3 months.\n"
            "FINDINGS:\n"
            "Brain parenchyma:\n"
            "No focal parenchymal signal abnormality is seen.\n"
            "IMPRESSION:\n"
            "Normal study of the brain.\n",
            ParseOptions(),
        )
        preview_template = collect_template(t_name)
        st.download_button(
            ":material/download: Download sample",
            data=build_docx(sample.blocks, template=preview_template),
            file_name=f"{safe_filename(t_name).replace('_HC_Format', '')}",
            mime=DOCX_MIME,
        )


with tab_rules:
    st.markdown(
        """
### HC FORMAT — implemented exactly

| # | Rule | Where it is enforced |
|---|------|----------------------|
| 1 | Arial, 12 pt, black only | `hc_format._style_run`, and on the `Normal` + `List Bullet` styles |
| 2 | Normal 1 inch margins, professional spacing — **1.5 line spacing** | `hc_format._base_document`, `hc_format.LINE_SPACING` |
| 3 | Title (first line only): centred, **BOLD**, <u>UNDERLINED</u>, UPPERCASE | `kind="title"` |
| 4 | Main headings: left, **BOLD**, <u>UNDERLINED</u>, UPPERCASE | `kind="heading"` |
| 5 | Findings: every finding a bullet; organ subheadings *italic* + <u>underlined</u>, **not** bold | `kind="bullet"` / `kind="subheading"` |
| 6 | Impression: every point a bullet, every bullet **BOLD** | `kind="bold_bullet"` |
| 7 | No changes, no summarising, no paraphrasing, no omissions, no altered measurements | rule-based parser + `verify.audit` |
| 8 | Delivered as .docx | `hc_format.build_docx` |

**Main headings recognised:** PATIENT NAME, AGE/SEX, EXAMINATION, CLINICAL HISTORY, TECHNIQUE,
IMAGING SEQUENCES USED, OBSERVATIONS, FINDINGS, IMPRESSION, CONCLUSION, COMMENT,
RECOMMENDATION, plus referral / date / comparison variants.

**Why the audit matters.** Rule 7 is the one that gets a radiologist into trouble, so it is
checked rather than assumed. Every word and number in your paste is counted, then counted again
from the finished .docx, and the two must match. Case is ignored (rules 3 and 4 force uppercase)
and bullet glyphs are ignored (Word draws its own). Anything else that differs is reported.
"""
    ,
        unsafe_allow_html=True,
    )
