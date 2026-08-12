"""
The air-gapped path: a local model served by Ollama on the clinic's own box.

A hospital that will not let patient text leave the building runs
`ollama serve` with a capable model (qwen2.5, llama3.1, medgemma...) on a
local GPU, sets two secrets -

    OLLAMA_URL   = "http://127.0.0.1:11434"
    OLLAMA_MODEL = "qwen2.5:7b-instruct"
    AI_PROVIDER  = "ollama"

- and the AI features run without any cloud at all. The provider registry
(providers.py) is the seam; nothing else in the app knows which vendor is
underneath.

Capabilities are honest: text drafting and impressions reuse the exact same
prompt builders (and the same negation tripwire) as the Gemini path. Vision
(scan pre-read, OCR) and speech stay unsupported here - the app hides those
features rather than pretending. The `api_key`/`model` arguments in each
signature exist to match the provider contract; the key is ignored and the
model falls back to OLLAMA_MODEL when the caller passes a Gemini model name.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

TIMEOUT = 120  # local models on modest GPUs are slow; honest > snappy


def config() -> dict | None:
    url = ""
    model = ""
    try:
        import streamlit as st

        url = str(st.secrets.get("OLLAMA_URL", "")).strip()
        model = str(st.secrets.get("OLLAMA_MODEL", "")).strip()
    except Exception:
        pass
    url = (url or os.environ.get("OLLAMA_URL", "")).strip().rstrip("/")
    model = (model or os.environ.get("OLLAMA_MODEL", "")).strip()
    if not url:
        return None
    return {"url": url, "model": model or "qwen2.5:7b-instruct"}


def _resolve_model(requested: str) -> str:
    """A Gemini model name passed through the shared call sites is not ours."""
    settings = config() or {}
    if requested and not requested.lower().startswith(("gemini", "models/")):
        return requested
    return settings.get("model", "")


def generate(prompt: str, *, system: str = "", model: str = "",
             json_mode: bool = False) -> str:
    """One completion from the local server. Raises RuntimeError, plainly."""
    settings = config()
    if not settings:
        raise RuntimeError("Ollama is not configured - set OLLAMA_URL in secrets.")
    payload: dict = {
        "model": _resolve_model(model),
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.0},
    }
    if system:
        payload["system"] = system
    if json_mode:
        payload["format"] = "json"
    request = urllib.request.Request(
        settings["url"] + "/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Ollama refused the request: HTTP {exc.code}") from exc
    except Exception as exc:
        raise RuntimeError(
            f"Could not reach Ollama at {settings['url']}: {exc}") from exc
    text = str(data.get("response") or "").strip()
    if not text:
        raise RuntimeError("Ollama returned an empty response.")
    return text


# ---- provider-contract implementations ------------------------------------ #


def draft_impression(findings_text: str, api_key: str, model: str,
                     template=None,
                     corpus_terms: list[str] | None = None) -> list[str]:
    """Same prompt, same JSON contract, same negation tripwire as Gemini."""
    import ai_parser
    import negation

    raw = generate(ai_parser.build_impression_prompt(findings_text, template,
                                                     corpus_terms),
                   model=model, json_mode=True)
    data = json.loads(raw)
    points = [str(p).strip().rstrip(".")
              for p in (data.get("impression") or []) if str(p).strip()]
    if not points:
        raise ValueError("The local model returned no impression points.")
    negation.assert_polarity(findings_text, "\n".join(points))
    return points


def draft_with_questions(raw_notes: str, template, api_key: str, model: str,
                         *, section: str = "the whole report",
                         answers: dict | None = None,
                         temperature: float = 0.2,
                         corpus_terms: list[str] | None = None) -> dict:
    """House-style drafting on the local model, negation-checked."""
    import ai_parser
    import negation

    raw = generate(
        ai_parser.build_draft_prompt(template, raw_notes, section, answers,
                                     corpus_terms),
        system=ai_parser.DRAFT_PROMPT + "\n\n" + ai_parser.ASK_PROMPT,
        model=model,
        json_mode=True,
    )
    data = json.loads(raw)
    draft = str(data.get("draft") or "").strip()
    negation.assert_polarity(raw_notes, draft)
    questions = []
    for q in data.get("questions") or []:
        if isinstance(q, dict) and str(q.get("question", "")).strip():
            questions.append({
                "id": str(q.get("id") or f"q{len(questions) + 1}"),
                "question": str(q["question"]).strip(),
                "why": str(q.get("why") or "").strip(),
                "options": [str(o).strip() for o in (q.get("options") or [])
                            if str(o).strip()],
            })
    return {
        "draft": draft,
        "questions": questions,
        "assumptions": [str(a).strip() for a in (data.get("assumptions") or [])
                        if str(a).strip()],
    }


def second_opinion(report_text: str, api_key: str, model: str) -> dict:
    """The safety second-opinion pass, air-gapped."""
    import ai_parser

    raw = generate(f"REPORT:\n{report_text.strip()}",
                   system=ai_parser.SECOND_OPINION_PROMPT,
                   model=model, json_mode=True)
    data = json.loads(raw)
    issues = []
    for item in data.get("issues") or []:
        if isinstance(item, dict) and str(item.get("title", "")).strip():
            severity = str(item.get("severity") or "note").lower()
            issues.append({
                "severity": severity if severity in ("critical", "warning", "note")
                else "note",
                "title": str(item["title"]).strip(),
                "detail": str(item.get("detail") or "").strip(),
            })
    return {"safe_to_send": bool(data.get("safe_to_send", not issues)),
            "issues": issues}
