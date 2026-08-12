"""
Offline checks for the clinic library (corpus.py).

Extraction must be deterministic and honest about why a term was kept;
persistence must be tenant-scoped, cap-enforced and impossible to confuse
with a template; the index must find near-misses at book scale without
slowing the Dictate tab; and no corpus argument may change a prompt that
did not ask for one.

    python corpus_check.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time

import corpus
import storage
import templates
from dictation_fix import RADLEX_CORE

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("  ok    " if condition else "  FAIL  ") + message)
    if not condition:
        failures.append(message)


CHAPTER = """
Cholangiocarcinoma commonly presents with painless jaundice. In this series,
cholangiocarcinoma was associated with pneumobilia in a minority of cases.
Portal vein thrombosis was seen in eleven patients; portal vein thrombosis
with cavernous transformation in three. Pneumobilia without prior
intervention should raise concern. The chapter discusses management, and the
results were considered significant by the authors. Cholangiocarcinoma
staging follows the AJCC system. Portal vein thrombosis alters resectability.
"""

# --------------------------------------------------------------------------- #
print("\nextraction - deterministic and selective")
# --------------------------------------------------------------------------- #

terms = corpus.extract_terms(CHAPTER, source="chapter.pdf")
names = {t.term.lower() for t in terms}
check("cholangiocarcinoma" in names, "cholangiocarcinoma extracted")
check("pneumobilia" in names, "pneumobilia extracted")
check("portal vein thrombosis" in names, "the phrase 'portal vein thrombosis' extracted")
check("chapter" not in names and "results" not in names and "authors" not in names,
      "report/academic filler excluded")
check("patients" not in names, "common English excluded")

again = corpus.extract_terms(CHAPTER, source="chapter.pdf")
check([(t.term, t.count) for t in terms] == [(a.term, a.count) for a in again],
      "extraction is deterministic - same text, same list, same order")

deduped = corpus.extract_terms(CHAPTER, source="chapter.pdf",
                               known={"cholangiocarcinoma"} | {t.lower() for t in RADLEX_CORE})
check("cholangiocarcinoma" not in {t.term.lower() for t in deduped},
      "known terms are not re-extracted")

reason = corpus.looks_medical("cholangiocarcinoma")
check(bool(reason), f"looks_medical gives a reason ({reason or 'none'})")
check(corpus.looks_medical("however") == "", "looks_medical rejects filler")

# --------------------------------------------------------------------------- #
print("\npersistence - tenant-scoped, capped, never a template")
# --------------------------------------------------------------------------- #

with tempfile.TemporaryDirectory() as tmp:
    store = storage.FileStore(tmp)
    real_get_store = storage.get_store
    storage.get_store = lambda: store
    try:
        built = corpus.add_source(corpus.Corpus(), "chapter.pdf", terms,
                                  added_by="check", sha1="abc")
        corpus.save(built, tenant="clinic_a")
        loaded = corpus.load(tenant="clinic_a")
        check(len(loaded.terms) == len(built.terms)
              and "chapter.pdf" in loaded.sources,
              "round trip through the store preserves terms and sources")
        check(len(corpus.load(tenant="clinic_b").terms) == 0,
              "tenant B cannot see tenant A's library")

        listed = templates.load_all(tenant="clinic_a")
        check(corpus.RESERVED_NAME not in listed,
              "the corpus record never appears as a template")
        check(templates.MAX_VOCABULARY == 300,
              "the per-doctor hot-list cap is untouched")

        try:
            bad = templates.copy_of(templates.HC_FORMAT, corpus.RESERVED_NAME)
            templates.save(bad, tenant="clinic_a")
            check(False, "a template took the reserved name")
        except ValueError:
            check(True, "templates.save refuses the reserved name")

        removed = corpus.remove_source(corpus.load(tenant="clinic_a"),
                                       "chapter.pdf")
        check(len(removed.terms) == 0 and not removed.sources,
              "removing the only source empties the library")
    finally:
        storage.get_store = real_get_store

check(len(corpus.from_dict("not a dict").terms) == 0,
      "corrupt payload loads as an empty corpus, no exception")

huge = {"name": corpus.RESERVED_NAME,
        "terms": [{"term": f"term{i:06d}xitis", "count": 1}
                  for i in range(corpus.MAX_CORPUS_TERMS + 5000)]}
check(len(corpus.from_dict(huge).terms) == corpus.MAX_CORPUS_TERMS,
      f"an oversized payload is capped at {corpus.MAX_CORPUS_TERMS} on load")

# --------------------------------------------------------------------------- #
print("\nthe index - near misses at book scale")
# --------------------------------------------------------------------------- #

index = corpus.TermIndex(
    ["cholelithiasis", "pneumobilia", "cholangiocarcinoma",
     "portal vein thrombosis"],
    {"cholelithiasis": 5},
)
found = index.near("cholelithisis")
check(found is not None and found[0] == "cholelithiasis",
      "a dropped letter still finds cholelithiasis")
found = index.near("coleli thiasis")
check(found is not None and found[0] == "cholelithiasis",
      "a split word still finds cholelithiasis")
check(index.near("cholelithiasis") is None,
      "a correct term is never suggested against itself")

big = corpus.TermIndex(f"finding{i:05d}osis" for i in range(20_000))
check(len(big) == 20_000, "twenty thousand terms indexed")
shortlist = big.candidates("finding00042osis")
check(0 < len(shortlist) <= 200,
      f"a probe touches a shortlist ({len(shortlist)} terms), not the corpus")

transcript = " ".join(f"word{i} finding{i:05d}oses" for i in range(100))
started = time.perf_counter()
corpus.suggest(transcript, big)
elapsed = time.perf_counter() - started
check(elapsed < 2.0, f"a 200-word pass over 20k terms takes {elapsed:.2f}s (< 2s)")

suggestions = corpus.suggest("patient has colangiocarcinoma today", index)
check(any(s.suggested == "cholangiocarcinoma" and s.reason == "corpus"
          for s in suggestions),
      "suggest() finds the near miss and tags it reason=corpus")
check(all(not hasattr(s, "text") for s in suggestions),
      "suggestions carry no rewritten text - applying is the user's act")

# --------------------------------------------------------------------------- #
print("\ntop-K selection - the whole corpus never enters a prompt")
# --------------------------------------------------------------------------- #

relevant = corpus.relevant_terms(
    "scan shows portal vein thrombosis and colangiocarcinoma", index, k=10)
check("portal vein thrombosis" in relevant, "an exact phrase is selected")
check("cholangiocarcinoma" in relevant, "a near-missed term is selected")
check("pneumobilia" not in relevant, "an unrelated term is not selected")
check(len(corpus.relevant_terms("normal study", big, k=15)) <= 15,
      "selection respects K")

hint = corpus.stt_hint("liver study", ["hepatosplenomegaly"], index,
                       max_chars=60)
check(len(hint) <= 60, "stt_hint respects its character budget")
check(hint.startswith("hepatosplenomegaly"),
      "the doctor's own hot list leads the hint")

# --------------------------------------------------------------------------- #
print("\nprompts - byte-identical unless a corpus is offered")
# --------------------------------------------------------------------------- #

import ai_parser

empty = templates.copy_of(templates.HC_FORMAT, "Corpus Check")
check(ai_parser.build_transcribe_prompt(empty)
      == ai_parser.build_transcribe_prompt(empty, corpus_terms=None),
      "transcribe prompt unchanged when no corpus is passed")
check("reference library" in
      ai_parser.build_transcribe_prompt(empty, corpus_terms=["pneumobilia"]),
      "transcribe prompt carries offered corpus terms")
check(ai_parser.build_draft_prompt(empty, "notes")
      == ai_parser.build_draft_prompt(empty, "notes", corpus_terms=None),
      "draft prompt unchanged when no corpus is passed")
draft_with = ai_parser.build_draft_prompt(empty, "notes",
                                          corpus_terms=["pneumobilia"])
check("pneumobilia" in draft_with and "SPELLING" in draft_with,
      "draft prompt carries corpus terms as spelling guidance only")
check(ai_parser.build_impression_prompt("findings")
      == ai_parser.build_impression_prompt("findings", corpus_terms=None),
      "impression prompt unchanged when no corpus is passed")
check("pneumobilia" in
      ai_parser.build_impression_prompt("findings",
                                        corpus_terms=["pneumobilia"]),
      "impression prompt carries offered corpus terms")

# --------------------------------------------------------------------------- #
print("\nSTT biasing - the hint reaches the wire only when non-empty")
# --------------------------------------------------------------------------- #

import http.server
import os
import socket
import threading

import stt


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Capture(http.server.BaseHTTPRequestHandler):
    last: bytes = b""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        type(self).last = self.rfile.read(length)
        answer = json.dumps({"text": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, *args):
        pass


port = _free_port()
server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Capture)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["CUSTOM_STT_URL"] = f"http://127.0.0.1:{port}/v1"
try:
    stt.transcribe("custom", b"fake-audio", "audio/webm",
                   vocab_hint="cholelithiasis, pneumobilia")
    check(b"cholelithiasis" in _Capture.last and b'name="prompt"' in _Capture.last,
          "a non-empty hint is sent as the Whisper prompt field")
    _Capture.last = b""
    stt.transcribe("custom", b"fake-audio", "audio/webm")
    check(b'name="prompt"' not in _Capture.last,
          "no hint, no prompt field - the wire shape is unchanged")
finally:
    os.environ.pop("CUSTOM_STT_URL", None)
    server.shutdown()

# --------------------------------------------------------------------------- #
summary = f"{len(failures)} failure(s)"
print("\n" + ("ALL CHECKS PASSED" if not failures else summary))
sys.exit(1 if failures else 0)
