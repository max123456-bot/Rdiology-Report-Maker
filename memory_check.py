"""
Offline checks for the agent memory (memory.py).

Embeddings must be deterministic offline; retrieval must rank by relevance,
fade with age, and never compare across embedder spaces; memories must be
tenant-scoped and invisible to the template list; the preference-pairs
archive must capture every real edit and export clean JSONL; and no memory
argument may change a prompt that did not ask for one.

    python memory_check.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta

import memory
import storage
import templates

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("  ok    " if condition else "  FAIL  ") + message)
    if not condition:
        failures.append(message)


# --------------------------------------------------------------------------- #
print("\nembeddings - deterministic, unit-norm, comparable")
# --------------------------------------------------------------------------- #

vec = memory.hash_embed("no pneumothorax or pleural effusion")
check(vec == memory.hash_embed("no pneumothorax or pleural effusion"),
      "the offline embedding is deterministic")
check(abs(sum(v * v for v in vec) - 1.0) < 1e-9, "vectors are unit-normalised")
check(memory.cosine(vec, vec) > 0.999, "cosine of a vector with itself is 1")
other = memory.hash_embed("completely unrelated words about invoices")
check(memory.cosine(vec, other) < 0.5, "unrelated text scores low")
made, embedder = memory.embed("test", api_key="")
check(embedder == "hash-256", "no key means the offline embedder, no network")

# --------------------------------------------------------------------------- #
print("\nthe memory store - scoped, capped, deduplicated")
# --------------------------------------------------------------------------- #

with tempfile.TemporaryDirectory() as tmp:
    store = storage.FileStore(tmp)
    real_get_store = storage.get_store
    storage.get_store = lambda: store
    try:
        memory.remember("Dr. A", "style_rule", "gallbladder wording",
                        "Never write normal gallbladder when calculi are present",
                        tenant="clinic_a")
        memory.remember("Dr. A", "episodic_case", "usg abdomen",
                        "IMPRESSION: 1. Cholelithiasis without cholecystitis.",
                        tenant="clinic_a")
        check(len(memory.load(tenant="clinic_a")) == 2,
              "memories round-trip through the store")
        check(len(memory.load(tenant="clinic_b")) == 0,
              "tenant B cannot see tenant A's memories")

        before = len(memory.load(tenant="clinic_a"))
        memory.remember("Dr. A", "style_rule", "gallbladder wording",
                        "Never write normal gallbladder when calculi are present",
                        tenant="clinic_a")
        check(len(memory.load(tenant="clinic_a")) == before,
              "the same lesson twice is re-confirmed, not duplicated")

        memory.remember("Dr. A", "style_rule", "gallbladder wording",
                        "Write cholelithiasis rather than gallstones",
                        tenant="clinic_a")
        old = [m for m in memory.load(tenant="clinic_a")
               if "Never write" in m.content][0]
        check(old.confidence == 0.5,
              "a new rule on the same concept halves the old rule's confidence")

        listed = templates.load_all(tenant="clinic_a")
        check(memory.MEMORY_NAME not in listed
              and memory.PAIRS_NAME not in listed,
              "memory records never appear as templates")
        try:
            bad = templates.copy_of(templates.HC_FORMAT, memory.MEMORY_NAME)
            templates.save(bad, tenant="clinic_a")
            check(False, "a template took a reserved memory name")
        except ValueError:
            check(True, "templates.save refuses the reserved memory names")

        # ------------------------------------------------------------------- #
        print("\nretrieval - relevance x recency x confidence")
        # ------------------------------------------------------------------- #

        hits = memory.retrieve("Dr. A", "gallbladder calculi seen on usg",
                               kind="style_rule", k=5, tenant="clinic_a")
        check(bool(hits), "a relevant rule is retrieved")
        check(all(m.kind == "style_rule" for m, _ in hits),
              "kind filter holds")
        check(not memory.retrieve("Dr. B", "gallbladder calculi",
                                  tenant="clinic_a"),
              "another doctor's query retrieves nothing of Dr. A's")

        bumped = [m for m in memory.load(tenant="clinic_a")
                  if m.content == hits[0][0].content][0]
        check(bumped.uses >= 1, "retrieval bumps the usage count")

        # An old memory scores lower than an identical fresh one.
        everything = memory.load(tenant="clinic_a")
        for m in everything:
            if "cholelithiasis rather" in m.content:
                stale = (datetime.now() - timedelta(days=720))
                m.created = m.last_used = stale.isoformat(timespec="seconds")
        memory.save(everything, tenant="clinic_a")
        aged = memory.retrieve("Dr. A", "write cholelithiasis rather than "
                               "gallstones", kind="style_rule",
                               tenant="clinic_a")
        aged_score = next((s for m, s in aged
                           if "cholelithiasis rather" in m.content), 0)
        memory.remember("Dr. A", "style_rule", "fresh twin",
                        "Write cholelithiasis rather than gallstones twin",
                        tenant="clinic_a")
        fresh = memory.retrieve("Dr. A", "write cholelithiasis rather than "
                                "gallstones", kind="style_rule",
                                tenant="clinic_a")
        fresh_score = next((s for m, s in fresh if "twin" in m.content), 0)
        check(0 < aged_score < fresh_score,
              f"two-year-old memory ({aged_score}) scores below a fresh one "
              f"({fresh_score})")

        block = memory.context_block("Dr. A", "gallbladder calculi usg",
                                     tenant="clinic_a")
        check("Remembered preferences" in block,
              "the context block carries retrieved rules")
        check(memory.context_block("Dr. Nobody", "anything",
                                   tenant="clinic_a") == "",
              "no memories, empty block - the prompt stays untouched")

        # ------------------------------------------------------------------- #
        print("\npreference pairs - the DPO archive")
        # ------------------------------------------------------------------- #

        memory.record_pair("Dr. A", "notes here", "the AI draft",
                           "the doctor's version", rule="a rule",
                           tenant="clinic_a")
        memory.record_pair("Dr. A", "notes", "same text", "same text",
                           tenant="clinic_a")
        pairs = memory.load_pairs(tenant="clinic_a")
        check(len(pairs) == 1, "an unchanged draft records nothing")
        check(pairs[0]["ai_draft"] == "the AI draft"
              and pairs[0]["final_edit"] == "the doctor's version",
              "the triple survives intact")
        exported = memory.export_pairs_jsonl(tenant="clinic_a")
        parsed = [json.loads(line) for line in exported.splitlines()]
        check(len(parsed) == 1 and parsed[0]["chosen"] == "the doctor's version"
              and parsed[0]["rejected"] == "the AI draft",
              "JSONL export is valid chosen/rejected training data")
    finally:
        storage.get_store = real_get_store

check(memory._from_payload("garbage") == [],
      "corrupt payload loads as no memories, no exception")
huge = {"memories": [{"doctor": "d", "kind": "style_rule", "concept": "c",
                      "content": f"rule {i}"}
                     for i in range(memory.MAX_MEMORIES + 500)]}
check(len(memory._from_payload(huge)) == memory.MAX_MEMORIES,
      f"an oversized payload is capped at {memory.MAX_MEMORIES} on load")

# --------------------------------------------------------------------------- #
print("\nprompts - byte-identical unless memory is offered")
# --------------------------------------------------------------------------- #

import ai_parser

empty = templates.copy_of(templates.HC_FORMAT, "Memory Check")
check(ai_parser.build_draft_prompt(empty, "notes")
      == ai_parser.build_draft_prompt(empty, "notes", memory_context=""),
      "draft prompt unchanged when no memory context is passed")
with_memory = ai_parser.build_draft_prompt(
    empty, "notes",
    memory_context="Remembered preferences of this radiologist:\n- a rule")
check("Remembered preferences" in with_memory,
      "draft prompt carries the memory block when offered")

# --------------------------------------------------------------------------- #
summary = f"{len(failures)} failure(s)"
print("\n" + ("ALL CHECKS PASSED" if not failures else summary))
sys.exit(1 if failures else 0)
