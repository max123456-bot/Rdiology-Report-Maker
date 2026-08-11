"""
Offline checks for the pluggable speech-to-text layer (stt.py).

Each provider is exercised against a loopback mock speaking its real wire
shape - Sarvam's transcript field and subscription-key header, ElevenLabs'
text field and xi-api-key header, the OpenAI-compatible form for custom
endpoints - so a wiring mistake fails here, not in a clinic.

    python stt_check.py
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import sys
import threading

import stt

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("  ok    " if condition else "  FAIL  ") + message)
    if not condition:
        failures.append(message)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def serve(handler_class):
    port = free_port()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler_class)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


AUDIO = b"\x1aE\xdf\xa3-fake-webm-audio-bytes"


class _Capture(http.server.BaseHTTPRequestHandler):
    last: dict = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        type(self).last = {
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        }
        answer = json.dumps(self.answer()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, *args):
        pass


# --------------------------------------------------------------------------- #
print("\nconfiguration honesty")
# --------------------------------------------------------------------------- #

for name in ("SARVAM_API_KEY", "ELEVENLABS_API_KEY", "CUSTOM_STT_URL"):
    os.environ.pop(name, None)
check(stt.available() == {}, "nothing configured, nothing offered")
try:
    stt.transcribe("sarvam", AUDIO, "audio/webm")
    check(False, "an unconfigured provider transcribed something")
except stt.SttError as exc:
    check("not configured" in str(exc), "an unconfigured provider says why")
try:
    stt.transcribe("nonexistent", AUDIO, "audio/webm")
    check(False, "an unknown provider was accepted")
except stt.SttError as exc:
    check("Unknown STT provider" in str(exc), "an unknown provider is refused")


# --------------------------------------------------------------------------- #
print("\nsarvam - wire shape")
# --------------------------------------------------------------------------- #


class MockSarvam(_Capture):
    def answer(self):
        return {"transcript": "यकृत सामान्य है liver is normal",
                "language_code": "hi-IN"}


server, port = serve(MockSarvam)
os.environ["SARVAM_API_KEY"] = "sk-test"
os.environ["SARVAM_STT_URL"] = f"http://127.0.0.1:{port}/speech-to-text"
try:
    check("sarvam" in stt.available(), "a configured Sarvam appears in the registry")
    result = stt.transcribe("sarvam", AUDIO, "audio/wav", language="hi")
    check("liver is normal" in result.text, "the code-mixed transcript comes back")
    check(result.provider == "Sarvam Saarika" and result.seconds > 0,
          "the result names the engine and carries measured latency")
    check(MockSarvam.last["headers"].get("api-subscription-key") == "sk-test",
          "auth goes in Sarvam's api-subscription-key header")
    check(b"saarika:v2.5" in MockSarvam.last["body"],
          "the default Saarika model is requested")
    check(b"hi-IN" in MockSarvam.last["body"],
          "the language becomes Sarvam's hi-IN code")
    check(AUDIO in MockSarvam.last["body"], "the audio bytes travel in the multipart body")

    result = stt.transcribe("sarvam", AUDIO, "audio/wav")
    check(b"unknown" in MockSarvam.last["body"],
          "no language means Saarika's own detection ('unknown')")
finally:
    os.environ.pop("SARVAM_API_KEY", None)
    os.environ.pop("SARVAM_STT_URL", None)
    server.shutdown()


# --------------------------------------------------------------------------- #
print("\nelevenlabs - wire shape")
# --------------------------------------------------------------------------- #


class MockEleven(_Capture):
    def answer(self):
        return {"text": "The liver is enlarged measuring sixteen centimeters",
                "language_code": "en"}


server, port = serve(MockEleven)
os.environ["ELEVENLABS_API_KEY"] = "xi-test"
os.environ["ELEVENLABS_STT_URL"] = f"http://127.0.0.1:{port}/v1/speech-to-text"
try:
    check("elevenlabs" in stt.available(), "a configured ElevenLabs appears")
    result = stt.transcribe("elevenlabs", AUDIO, "audio/webm")
    check("liver is enlarged" in result.text, "the transcript comes back")
    check(MockEleven.last["headers"].get("xi-api-key") == "xi-test",
          "auth goes in the xi-api-key header")
    check(b"scribe_v1" in MockEleven.last["body"], "Scribe is the default model")
    check(AUDIO in MockEleven.last["body"],
          "the WebM goes up untouched - no ffmpeg transcode on this path")
finally:
    os.environ.pop("ELEVENLABS_API_KEY", None)
    os.environ.pop("ELEVENLABS_STT_URL", None)
    server.shutdown()


# --------------------------------------------------------------------------- #
print("\ncustom - any OpenAI-compatible endpoint")
# --------------------------------------------------------------------------- #


class MockWhisper(_Capture):
    def answer(self):
        return {"text": "No focal lesion is seen"}


server, port = serve(MockWhisper)
os.environ["CUSTOM_STT_URL"] = f"http://127.0.0.1:{port}/v1"
os.environ["CUSTOM_STT_KEY"] = "gsk-test"
os.environ["CUSTOM_STT_MODEL"] = "whisper-large-v3-turbo"
os.environ["CUSTOM_STT_NAME"] = "Groq Whisper"
try:
    offered = stt.available()
    check("custom" in offered and "Groq Whisper" in offered["custom"],
          "the custom endpoint appears under its own name")
    result = stt.transcribe("custom", AUDIO, "audio/webm", language="en")
    check(result.text == "No focal lesion is seen", "the transcript comes back")
    check(MockWhisper.last["path"].endswith("/v1/audio/transcriptions"),
          "the OpenAI /audio/transcriptions path is appended to a bare base URL")
    check(MockWhisper.last["headers"].get("authorization") == "Bearer gsk-test",
          "auth is a Bearer token")
    check(b"whisper-large-v3-turbo" in MockWhisper.last["body"],
          "the configured model is requested")
finally:
    for name in ("CUSTOM_STT_URL", "CUSTOM_STT_KEY", "CUSTOM_STT_MODEL",
                 "CUSTOM_STT_NAME"):
        os.environ.pop(name, None)
    server.shutdown()


# --------------------------------------------------------------------------- #
print("\nfailure honesty")
# --------------------------------------------------------------------------- #

os.environ["ELEVENLABS_API_KEY"] = "xi-test"
os.environ["ELEVENLABS_STT_URL"] = f"http://127.0.0.1:{free_port()}/v1/speech-to-text"
try:
    stt.transcribe("elevenlabs", AUDIO, "audio/webm")
    check(False, "a dead endpoint did not raise")
except stt.SttError as exc:
    check("Could not reach" in str(exc), "a dead endpoint fails plainly")
finally:
    os.environ.pop("ELEVENLABS_API_KEY", None)
    os.environ.pop("ELEVENLABS_STT_URL", None)


class MockSilent(_Capture):
    def answer(self):
        return {"text": ""}


server, port = serve(MockSilent)
os.environ["CUSTOM_STT_URL"] = f"http://127.0.0.1:{port}/v1"
try:
    stt.transcribe("custom", AUDIO, "audio/webm")
    check(False, "an empty transcript was accepted")
except stt.SttError as exc:
    check("no text" in str(exc), "an empty transcript is refused with a reason")
finally:
    os.environ.pop("CUSTOM_STT_URL", None)
    server.shutdown()


# --------------------------------------------------------------------------- #
print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All STT checks passed.")
