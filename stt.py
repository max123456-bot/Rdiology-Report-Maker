"""
Pluggable speech-to-text: bring whichever engine your clinic bought.

The lag in the old pipeline had two sources: a cold-starting Hugging Face
model (up to a minute) and an ffmpeg transcode of every recording. Modern
hosted ASR fixes both - purpose-built endpoints answer short clips in about
a second and most of them accept the browser's WebM/Opus directly, so the
transcode disappears too.

Built-in providers, each active the moment its key is configured:

    sarvam       Sarvam AI "Saarika" - Indian STT, code-mixed Hindi/English,
                 the best latency/accuracy for Indian dictation today.
                 SARVAM_API_KEY (+ SARVAM_STT_MODEL, default saarika:v2.5)

    elevenlabs   ElevenLabs "Scribe" - strong multilingual ASR, takes WebM
                 directly. ELEVENLABS_API_KEY (+ ELEVENLABS_STT_MODEL)

    custom       ANY OpenAI-compatible /audio/transcriptions endpoint: Groq
                 Whisper, OpenAI, a local faster-whisper server, whatever
                 comes next. CUSTOM_STT_URL (+ CUSTOM_STT_KEY,
                 CUSTOM_STT_MODEL, CUSTOM_STT_NAME for the UI label)

The existing engines (Gemini one-pass, AI4Bharat hosted/local) stay in
ai_parser.py / speech.py; this module only adds the new front ends. Every
result carries its measured latency, so the doctor can SEE which engine is
fast instead of taking anyone's word for it. Whatever the engine, the text
still goes through dictation_fix (ITN, lexicon suggestions) and, when
paired with Gemini, the same layout pass - the safety pipeline does not
care who did the listening.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

TIMEOUT = 60


def _secret(name: str, default: str = "") -> str:
    try:
        import streamlit as st

        if name in st.secrets:
            return str(st.secrets[name]).strip()
    except Exception:
        pass
    return os.environ.get(name, default).strip()


@dataclass
class SttResult:
    text: str
    provider: str
    model: str = ""
    language: str = ""
    seconds: float = 0.0

    @property
    def note(self) -> str:
        return (f"Transcribed by {self.provider}"
                + (f" ({self.model})" if self.model else "")
                + (f" in {self.seconds:.1f}s." if self.seconds else "."))


class SttError(RuntimeError):
    """Anything that stopped audio becoming text, phrased for a human."""


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #


def _sarvam_config() -> dict | None:
    key = _secret("SARVAM_API_KEY")
    if not key:
        return None
    return {"key": key, "model": _secret("SARVAM_STT_MODEL", "saarika:v2.5"),
            "url": _secret("SARVAM_STT_URL", "https://api.sarvam.ai/speech-to-text")}


def _transcribe_sarvam(audio: bytes, mime: str, language: str) -> SttResult:
    import requests

    config = _sarvam_config()
    if not config:
        raise SttError("Sarvam is not configured - set SARVAM_API_KEY in secrets.")

    # Saarika wants WAV/MP3; the browser records WebM, so this one converts.
    import speech

    wav, wav_mime = speech.to_wav(audio, mime)
    data = {"model": config["model"]}
    # "unknown" turns on Saarika's own language detection, which is what a
    # Hindi/English code-mixed dictation actually needs.
    data["language_code"] = f"{language}-IN" if language else "unknown"

    started = time.perf_counter()
    try:
        response = requests.post(
            config["url"],
            headers={"api-subscription-key": config["key"]},
            files={"file": ("dictation.wav", wav, wav_mime)},
            data=data,
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise SttError(f"Could not reach Sarvam: {exc}") from exc
    if response.status_code in (401, 403):
        raise SttError("Sarvam rejected the key - check SARVAM_API_KEY.")
    if response.status_code != 200:
        raise SttError(f"Sarvam returned {response.status_code}: "
                       f"{response.text[:300]}")
    payload = response.json()
    text = str(payload.get("transcript") or "").strip()
    if not text:
        raise SttError("Sarvam returned no text - the recording may be silent.")
    return SttResult(text=text, provider="Sarvam Saarika", model=config["model"],
                     language=str(payload.get("language_code") or language),
                     seconds=time.perf_counter() - started)


def _elevenlabs_config() -> dict | None:
    key = _secret("ELEVENLABS_API_KEY")
    if not key:
        return None
    return {"key": key, "model": _secret("ELEVENLABS_STT_MODEL", "scribe_v1"),
            "url": _secret("ELEVENLABS_STT_URL",
                           "https://api.elevenlabs.io/v1/speech-to-text")}


def _transcribe_elevenlabs(audio: bytes, mime: str, language: str) -> SttResult:
    import requests

    config = _elevenlabs_config()
    if not config:
        raise SttError("ElevenLabs is not configured - set ELEVENLABS_API_KEY.")

    data = {"model_id": config["model"], "tag_audio_events": "false"}
    if language:
        data["language_code"] = language

    started = time.perf_counter()
    try:
        # Scribe takes the browser's WebM directly - no transcode, no ffmpeg.
        response = requests.post(
            config["url"],
            headers={"xi-api-key": config["key"]},
            files={"file": ("dictation.webm", audio, mime or "audio/webm")},
            data=data,
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise SttError(f"Could not reach ElevenLabs: {exc}") from exc
    if response.status_code in (401, 403):
        raise SttError("ElevenLabs rejected the key - check ELEVENLABS_API_KEY.")
    if response.status_code != 200:
        raise SttError(f"ElevenLabs returned {response.status_code}: "
                       f"{response.text[:300]}")
    payload = response.json()
    text = str(payload.get("text") or "").strip()
    if not text:
        raise SttError("ElevenLabs returned no text - the recording may be silent.")
    return SttResult(text=text, provider="ElevenLabs Scribe",
                     model=config["model"],
                     language=str(payload.get("language_code") or language),
                     seconds=time.perf_counter() - started)


def _custom_config() -> dict | None:
    url = _secret("CUSTOM_STT_URL").rstrip("/")
    if not url:
        return None
    if not url.endswith("/audio/transcriptions"):
        url += "/audio/transcriptions"
    return {
        "url": url,
        "key": _secret("CUSTOM_STT_KEY"),
        "model": _secret("CUSTOM_STT_MODEL", "whisper-large-v3"),
        "name": _secret("CUSTOM_STT_NAME", "Custom STT"),
    }


def _transcribe_custom(audio: bytes, mime: str, language: str) -> SttResult:
    import requests

    config = _custom_config()
    if not config:
        raise SttError("No custom STT endpoint - set CUSTOM_STT_URL in secrets.")

    headers = {}
    if config["key"]:
        headers["Authorization"] = f"Bearer {config['key']}"
    data = {"model": config["model"], "response_format": "json"}
    if language:
        data["language"] = language

    started = time.perf_counter()
    try:
        response = requests.post(
            config["url"], headers=headers,
            files={"file": ("dictation.webm", audio, mime or "audio/webm")},
            data=data, timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise SttError(f"Could not reach {config['name']}: {exc}") from exc
    if response.status_code in (401, 403):
        raise SttError(f"{config['name']} rejected the key - check CUSTOM_STT_KEY.")
    if response.status_code != 200:
        raise SttError(f"{config['name']} returned {response.status_code}: "
                       f"{response.text[:300]}")
    payload = response.json()
    text = str(payload.get("text") or "").strip()
    if not text:
        raise SttError(f"{config['name']} returned no text.")
    return SttResult(text=text, provider=config["name"], model=config["model"],
                     language=language, seconds=time.perf_counter() - started)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

_BUILTINS = {
    "sarvam": (_sarvam_config, _transcribe_sarvam,
               "Sarvam Saarika — Indian STT, Hindi/English code-mixed, ~1s"),
    "elevenlabs": (_elevenlabs_config, _transcribe_elevenlabs,
                   "ElevenLabs Scribe — multilingual, WebM direct, fast"),
    "custom": (_custom_config, _transcribe_custom, ""),  # label from config
}


def available() -> dict[str, str]:
    """
    provider-id -> UI label, for every provider whose key is configured.
    Unconfigured providers simply do not appear - no dead menu entries.
    """
    out: dict[str, str] = {}
    for name, (config_fn, _, label) in _BUILTINS.items():
        config = config_fn()
        if config is None:
            continue
        if name == "custom":
            label = (f"{config['name']} — your own endpoint "
                     f"({config['model']})")
        out[name] = label
    return out


def transcribe(provider: str, audio: bytes, mime: str,
               language: str = "") -> SttResult:
    """One recording through the chosen provider. Raises SttError, plainly."""
    entry = _BUILTINS.get(provider)
    if entry is None:
        raise SttError(f"Unknown STT provider “{provider}”. "
                       f"Configured: {', '.join(available()) or 'none'}.")
    return entry[1](audio, mime, language)
