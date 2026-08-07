"""
Speech-to-text backends for the Dictate tab.

Two families, and they are good at different things:

  Gemini          hears the audio AND lays the report out in one pass - punctuation,
                  headings, self-corrections, uncertainty flags. Weaker on strong Indian
                  accents and on Hindi/English code-mixing.

  AI4Bharat       Indic ASR from IIT Madras, trained on Indian speech across 22 languages.
                  Much better at the acoustics for an Indian doctor. Returns bare text:
                  no punctuation, no structure, no uncertainty.

So the best setting for most Indian clinics is AI4Bharat for the listening and Gemini for
the layout - `ai4bharat+gemini`. Each does the half it is actually good at.

Model IDs are never hardcoded as gospel: AI4Bharat publish and rename repositories on
Hugging Face regularly, so the UI offers presets and lets you paste any repo ID.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

# Presets are a convenience, not a promise. Verify a repo on huggingface.co before
# relying on it; the UI accepts any ID you paste.
AI4BHARAT_PRESETS = [
    "ai4bharat/indicwhisper",
    "ai4bharat/indic-conformer-600m-multilingual",
    "ai4bharat/indicwav2vec-hindi",
    "ai4bharat/indicwav2vec_v1_bengali",
]

# The 22 scheduled languages AI4Bharat targets, plus Indian English.
LANGUAGES = {
    "en": "English (Indian)",
    "hi": "Hindi",
    "bn": "Bengali",
    "mr": "Marathi",
    "te": "Telugu",
    "ta": "Tamil",
    "gu": "Gujarati",
    "kn": "Kannada",
    "ml": "Malayalam",
    "pa": "Punjabi",
    "or": "Odia",
    "as": "Assamese",
    "ur": "Urdu",
    "ne": "Nepali",
    "sa": "Sanskrit",
    "sd": "Sindhi",
    "kok": "Konkani",
    "mai": "Maithili",
    "mni": "Manipuri",
    "brx": "Bodo",
    "doi": "Dogri",
    "ks": "Kashmiri",
    "sat": "Santali",
}

ENGINES = {
    "gemini": "Gemini — hears and lays out the report in one pass",
    "ai4bharat+gemini": "AI4Bharat + Gemini — Indic listening, Gemini layout (best for Indian accents)",
    "ai4bharat": "AI4Bharat only — raw text, no layout, runs offline if installed locally",
}


class SpeechError(RuntimeError):
    """Anything that stopped audio becoming text, phrased for the person reading it."""


# A Hugging Face repo ID is owner/name. Anything else - a path traversal, a full
# URL, a host - must not reach the request URL or the local loader.
_MODEL_ID = __import__("re").compile(r"^[A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*$")


def validate_model_id(model: str) -> str:
    """Reject anything that is not a plain owner/name repository ID."""
    clean = (model or "").strip().strip("/")
    if not _MODEL_ID.match(clean) or ".." in clean:
        raise SpeechError(
            f"“{model}” is not a valid Hugging Face model ID. It should look like "
            "`ai4bharat/indicwhisper` — owner, slash, model name, nothing else."
        )
    return clean


@dataclass
class Transcription:
    text: str
    engine: str
    model: str = ""
    language: str = ""
    note: str = ""


# --------------------------------------------------------------------------- #
# Audio plumbing
# --------------------------------------------------------------------------- #


def to_wav(audio_bytes: bytes, mime_type: str) -> tuple[bytes, str]:
    """
    Convert browser audio to 16 kHz mono WAV, which every ASR stack accepts.

    The browser records WebM/Opus. Gemini takes that directly, but Hugging Face
    and local models generally want WAV. Returns the audio unchanged if it is
    already WAV, or if no converter is installed - the caller then finds out from
    the backend rather than from a guess made here.
    """
    if "wav" in (mime_type or "").lower():
        return audio_bytes, "audio/wav"

    try:
        from pydub import AudioSegment
    except ImportError:
        return audio_bytes, mime_type or "audio/webm"

    try:
        sound = AudioSegment.from_file(io.BytesIO(audio_bytes))
        sound = sound.set_frame_rate(16000).set_channels(1).set_sample_width(2)
        out = io.BytesIO()
        sound.export(out, format="wav")
        return out.getvalue(), "audio/wav"
    except Exception as exc:
        raise SpeechError(
            "Could not convert the recording to WAV. This needs ffmpeg on your PATH "
            f"(install it, then restart the app). Underlying error: {exc}"
        ) from exc


# --------------------------------------------------------------------------- #
# AI4Bharat via the Hugging Face Inference API
# --------------------------------------------------------------------------- #


def transcribe_ai4bharat_hf(
    audio_bytes: bytes,
    mime_type: str,
    *,
    model: str,
    hf_token: str,
    language: str = "hi",
    timeout: int = 120,
) -> Transcription:
    """Send the audio to Hugging Face's hosted inference for an AI4Bharat model."""
    import json
    import urllib.error
    import urllib.request

    if not hf_token:
        raise SpeechError(
            "No Hugging Face token configured. Add HF_TOKEN to .streamlit/secrets.toml "
            "(a free token from huggingface.co/settings/tokens)."
        )

    clean_model = validate_model_id(model)
    wav, wav_mime = to_wav(audio_bytes, mime_type)
    url = f"https://api-inference.huggingface.co/models/{clean_model}"
    request = urllib.request.Request(
        url,
        data=wav,
        headers={
            "Authorization": f"Bearer {hf_token}",
            "Content-Type": wav_mime,
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        if exc.code == 404:
            raise SpeechError(
                f"Hugging Face has no servable model at “{model}”. AI4Bharat rename and "
                "retire repositories often - check the exact ID on huggingface.co and paste "
                "it in, or switch to a local install."
            ) from exc
        if exc.code == 503:
            raise SpeechError(
                f"“{model}” is loading on Hugging Face (this can take a minute on a cold "
                "start). Try again shortly."
            ) from exc
        if exc.code in (401, 403):
            raise SpeechError(
                "Hugging Face rejected the token. Check HF_TOKEN, and that the token has "
                "access to this model."
            ) from exc
        raise SpeechError(f"Hugging Face returned {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise SpeechError(f"Could not reach Hugging Face: {exc.reason}") from exc

    text = ""
    if isinstance(payload, dict):
        text = str(payload.get("text") or "")
        if not text and payload.get("error"):
            raise SpeechError(f"Hugging Face: {payload['error']}")
    elif isinstance(payload, list) and payload:
        first = payload[0]
        text = str(first.get("text", "") if isinstance(first, dict) else first)

    if not text.strip():
        raise SpeechError("The model returned no text. The recording may be silent or too short.")

    return Transcription(
        text=text.strip(), engine="ai4bharat", model=model, language=language,
        note="Transcribed by AI4Bharat via Hugging Face.",
    )


# --------------------------------------------------------------------------- #
# AI4Bharat running locally
# --------------------------------------------------------------------------- #

_LOCAL_PIPELINES: dict[str, object] = {}


def transcribe_ai4bharat_local(
    audio_bytes: bytes,
    mime_type: str,
    *,
    model: str,
    language: str = "hi",
    allow_remote_code: bool = False,
) -> Transcription:
    """
    Run an AI4Bharat model on this machine.

    Nothing leaves the building, which matters for identifiable patient dictation.
    The first run downloads the model (often over a gigabyte); afterwards it is
    cached and works with no internet at all. The loaded pipeline is kept in memory
    so the second dictation does not pay the load cost again.

    `allow_remote_code` is off by default and should stay off. Turning it on lets
    the model repository execute its own Python on this machine during loading -
    fine for a repo you trust, a full compromise for one you do not, and the repo
    ID is a free-text field.
    """
    validate_model_id(model)

    try:
        from transformers import pipeline
    except ImportError as exc:
        raise SpeechError(
            "Local AI4Bharat needs PyTorch and Transformers:\n\n"
            "    pip install torch transformers\n\n"
            "Or use the Hugging Face option, which needs no install."
        ) from exc

    wav, _ = to_wav(audio_bytes, mime_type)

    cache_key = f"{model}|{allow_remote_code}"
    asr = _LOCAL_PIPELINES.get(cache_key)
    if asr is None:
        # One model in memory at a time; these are gigabytes each.
        _LOCAL_PIPELINES.clear()
        try:
            asr = pipeline(
                "automatic-speech-recognition",
                model=model,
                trust_remote_code=allow_remote_code,
            )
        except Exception as exc:
            hint = (
                ""
                if allow_remote_code
                else "\n\nIf the model card says it needs custom code, tick “allow this model "
                     "to run its own code” — but only for a repository you trust, because that "
                     "lets it execute Python on this machine."
            )
            raise SpeechError(
                f"Could not load “{model}” locally: {exc}\n\n"
                "Whisper-family AI4Bharat models load with a plain pipeline; some Conformer "
                "models need NVIDIA NeMo instead. Check the model card on huggingface.co."
                + hint
            ) from exc
        _LOCAL_PIPELINES[cache_key] = asr

    try:
        kwargs = {}
        if language and "whisper" in model.lower():
            kwargs["generate_kwargs"] = {"language": language, "task": "transcribe"}
        result = asr(wav, **kwargs)
    except Exception as exc:
        raise SpeechError(f"Local transcription failed: {exc}") from exc

    text = str(result.get("text", "") if isinstance(result, dict) else result).strip()
    if not text:
        raise SpeechError("The model returned no text. The recording may be silent or too short.")

    return Transcription(
        text=text, engine="ai4bharat", model=model, language=language,
        note="Transcribed locally by AI4Bharat - the audio never left this machine.",
    )


def transcribe_ai4bharat(
    audio_bytes: bytes,
    mime_type: str,
    *,
    model: str,
    language: str = "hi",
    hf_token: str = "",
    run_locally: bool = False,
    allow_remote_code: bool = False,
) -> Transcription:
    if run_locally:
        return transcribe_ai4bharat_local(
            audio_bytes, mime_type, model=model, language=language,
            allow_remote_code=allow_remote_code,
        )
    return transcribe_ai4bharat_hf(
        audio_bytes, mime_type, model=model, hf_token=hf_token, language=language
    )
