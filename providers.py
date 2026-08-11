"""
The AI provider registry - one seam between the app and any model vendor.

CARPL sells hospitals a single integration layer that can route to hundreds
of third-party models. This is that seam at this project's honest scale: the
app asks for a capability ("draft an impression", "pre-read this scan"), the
registry hands it the active provider's implementation, and nothing outside
this file knows whose API is underneath.

Gemini is the built-in provider, assembled from ai_parser. Another vendor -
a hospital's on-prem model, a different cloud - plugs in by registering a
Provider whose callables have the same signatures; no other file changes.

Selection: the AI_PROVIDER secret (or environment variable) names the active
provider; absent, "gemini". An unknown name falls back to gemini and says so,
because a silently-dead AI path is worse than a loud one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# Signatures every provider implements. A provider may leave a capability as
# None; the app hides the matching feature instead of crashing.
#
#   structure(raw_text, api_key, model) -> list[Block]
#   ocr(file_bytes, mime_type, api_key, model) -> str
#   draft(prompt-args...) - see ai_parser.draft_with_questions
#   impression(findings_text, api_key, model, template) -> list[str]
#   prefill(file_bytes, mime_type, api_key, model, context) -> dict
#   transcribe(audio..., api_key, model) - see ai_parser.transcribe_dictation
#   review(transcript, template, api_key, model) -> list[dict]


@dataclass
class Provider:
    name: str
    label: str
    structure: Callable | None = None
    ocr: Callable | None = None
    draft: Callable | None = None
    impression: Callable | None = None
    prefill: Callable | None = None
    transcribe: Callable | None = None
    review: Callable | None = None
    extras: dict = field(default_factory=dict)

    def supports(self, capability: str) -> bool:
        return getattr(self, capability, None) is not None


_registry: dict[str, Provider] = {}


def register(provider: Provider) -> None:
    _registry[provider.name] = provider


def _gemini() -> Provider:
    import ai_parser

    return Provider(
        name="gemini",
        label="Google Gemini",
        structure=ai_parser.structure_with_ai,
        ocr=ai_parser.extract_text_from_file,
        draft=ai_parser.draft_with_questions,
        impression=ai_parser.draft_impression,
        prefill=ai_parser.prefill_from_scan,
        transcribe=ai_parser.transcribe_dictation,
        review=ai_parser.review_transcript,
    )


def _configured_name() -> str:
    import os

    try:
        import streamlit as st

        if "AI_PROVIDER" in st.secrets:
            return str(st.secrets["AI_PROVIDER"]).strip().lower()
    except Exception:
        pass
    return os.environ.get("AI_PROVIDER", "gemini").strip().lower() or "gemini"


def available() -> list[str]:
    if "gemini" not in _registry:
        register(_gemini())
    return sorted(_registry)


def active() -> Provider:
    """The provider the app should use right now."""
    if "gemini" not in _registry:
        register(_gemini())
    name = _configured_name()
    if name in _registry:
        return _registry[name]
    provider = _registry["gemini"]
    provider.extras["warning"] = (
        f"AI_PROVIDER is set to “{name}” but no such provider is registered - "
        "using Gemini."
    )
    return provider
