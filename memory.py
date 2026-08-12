"""
Agentic memory: what the app remembers about each doctor, retrievably.

Three tiers, honest about what each is:

  Working memory   the current Streamlit session - already handled by
                   st.session_state; nothing to build.
  Semantic memory  style rules learned from the doctor's edits. The always-on
                   subset lives in Template.preferences (capped, always sent);
                   THIS module keeps the uncapped archive with embeddings, and
                   retrieves only the rules relevant to the notes at hand.
  Episodic memory  past accepted reports, retrieved by similarity so a draft
                   of a fatty-liver study can lean on how this doctor phrased
                   the last one.

Retrieval scoring: cosine similarity x exponential recency decay x confidence.
A rule the doctor contradicted fades; a rule that keeps getting used stays.

Embeddings: Gemini (gemini-embedding-001) when a key is offered, otherwise a
deterministic hashed bag-of-words vector - offline, no key, testable. Each
memory records which embedder made its vector; vectors are only compared to
queries embedded the same way, and keyword overlap covers the rest. So the
app degrades gracefully instead of comparing incomparable spaces.

Storage rides the pluggable store as reserved payloads (like corpus.py):
"__agent_memory__" and "__preference_pairs__", one each per tenant. The
preference-pairs archive is the DPO dataset: every (notes, AI draft, doctor
final) triple, exportable as JSONL for future fine-tuning.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime

import storage

MEMORY_NAME = "__agent_memory__"
PAIRS_NAME = "__preference_pairs__"
CHATS_NAME = "__chat_history__"
RESERVED_NAMES = (MEMORY_NAME, PAIRS_NAME, CHATS_NAME)

MAX_MEMORIES = 2000
MAX_PAIRS = 2000
MAX_CHATS = 500
MAX_CONTENT_CHARS = 4000

# Recency decay: half-life of ~180 days. Old style preferences fade unless
# they keep being retrieved and re-confirmed.
DECAY_LAMBDA = math.log(2) / 180.0

HASH_DIM = 256
KINDS = ("style_rule", "episodic_case", "knowledge")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Embeddings - Gemini when offered a key, hashed bag-of-words otherwise
# --------------------------------------------------------------------------- #

_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]{2,}|\d+(?:\.\d+)?")


def hash_embed(text: str) -> list[float]:
    """
    A deterministic offline embedding: every token hashed into one of
    HASH_DIM buckets, term-frequency weighted, L2-normalised. Not semantic -
    but stable, keyless, and good enough to rank rules by shared vocabulary.
    """
    vec = [0.0] * HASH_DIM
    for token in _WORD.findall((text or "").lower()):
        bucket = int(hashlib.sha1(token.encode("utf-8")).hexdigest(), 16) % HASH_DIM
        vec[bucket] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


def gemini_embed(texts: list[str], api_key: str,
                 model: str = "gemini-embedding-001") -> list[list[float]]:
    """Real semantic vectors. Raises on any failure - the caller falls back."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    result = client.models.embed_content(
        model=model,
        contents=texts,
        config=types.EmbedContentConfig(output_dimensionality=HASH_DIM),
    )
    out = []
    for emb in result.embeddings:
        values = list(emb.values)
        norm = math.sqrt(sum(v * v for v in values))
        out.append([v / norm for v in values] if norm else values)
    return out


def embed(text: str, api_key: str = "") -> tuple[list[float], str]:
    """The vector and the name of the embedder that made it."""
    if api_key:
        try:
            return gemini_embed([text], api_key)[0], "gemini-256"
        except Exception:
            pass  # offline, quota, bad key - the hash vector still works
    return hash_embed(text), "hash-256"


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))   # both are already unit-norm


def _keyword_overlap(query: str, content: str) -> float:
    """Fallback score when embedder spaces do not match."""
    q = {t.lower() for t in _WORD.findall(query)}
    c = {t.lower() for t in _WORD.findall(content)}
    if not q or not c:
        return 0.0
    return len(q & c) / math.sqrt(len(q) * len(c))


# --------------------------------------------------------------------------- #
# The memory store
# --------------------------------------------------------------------------- #


@dataclass
class Memory:
    doctor: str                    # template name the memory belongs to
    kind: str                      # style_rule | episodic_case | knowledge
    concept: str                   # short key: what this memory is about
    content: str                   # the rule / case summary itself
    vector: list[float] = field(default_factory=list)
    embedder: str = "hash-256"
    confidence: float = 1.0
    uses: int = 0
    created: str = ""
    last_used: str = ""


def _to_payload(memories: list[Memory]) -> dict:
    return {
        "name": MEMORY_NAME,
        "kind": "agent_memory",
        "updated": _now(),
        "memories": [
            {"doctor": m.doctor, "kind": m.kind, "concept": m.concept,
             "content": m.content, "vector": m.vector, "embedder": m.embedder,
             "confidence": m.confidence, "uses": m.uses,
             "created": m.created, "last_used": m.last_used}
            for m in memories
        ],
    }


def _from_payload(payload) -> list[Memory]:
    out: list[Memory] = []
    if not isinstance(payload, dict):
        return out
    for entry in (payload.get("memories") or [])[:MAX_MEMORIES]:
        if not isinstance(entry, dict):
            continue
        content = str(entry.get("content", "")).strip()[:MAX_CONTENT_CHARS]
        kind = str(entry.get("kind", ""))
        if not content or kind not in KINDS:
            continue
        vector = entry.get("vector")
        if not isinstance(vector, list):
            vector = []
        try:
            confidence = min(1.0, max(0.0, float(entry.get("confidence", 1.0))))
        except (TypeError, ValueError):
            confidence = 1.0
        out.append(Memory(
            doctor=str(entry.get("doctor", "")).strip(),
            kind=kind,
            concept=str(entry.get("concept", "")).strip()[:200],
            content=content,
            vector=[float(v) for v in vector if isinstance(v, (int, float))],
            embedder=str(entry.get("embedder", "hash-256")),
            confidence=confidence,
            uses=max(0, int(entry.get("uses", 0) or 0)),
            created=str(entry.get("created", "")),
            last_used=str(entry.get("last_used", "")),
        ))
    return out


def load(tenant: str | None = None) -> list[Memory]:
    scope = tenant or storage.current_tenant()
    try:
        payload = storage.get_store().load_all(scope).get(MEMORY_NAME)
    except Exception:
        return []
    return _from_payload(payload)


def save(memories: list[Memory], tenant: str | None = None) -> None:
    scope = tenant or storage.current_tenant()
    trimmed = memories[-MAX_MEMORIES:]
    storage.get_store().save(scope, MEMORY_NAME, _to_payload(trimmed))


def remember(doctor: str, kind: str, concept: str, content: str, *,
             api_key: str = "", tenant: str | None = None) -> Memory:
    """
    Store one memory. A duplicate (same doctor, kind, near-identical content)
    is re-confirmed - confidence restored, timestamp refreshed - instead of
    stored twice. A new rule under the SAME concept halves the confidence of
    older rules on that concept: the newest instruction wins, gradually.
    """
    if kind not in KINDS:
        raise ValueError(f"Unknown memory kind: {kind}")
    clean = content.strip()[:MAX_CONTENT_CHARS]
    if not clean:
        raise ValueError("An empty memory teaches nothing.")

    memories = load(tenant)
    norm = clean.rstrip(".").lower()
    for existing in memories:
        if (existing.doctor == doctor and existing.kind == kind
                and existing.content.rstrip(".").lower() == norm):
            existing.confidence = 1.0
            existing.last_used = _now()
            save(memories, tenant)
            return existing

    key = concept.strip().lower()
    if key and kind == "style_rule":
        for existing in memories:
            if (existing.doctor == doctor and existing.kind == kind
                    and existing.concept.strip().lower() == key):
                existing.confidence *= 0.5   # superseded, not erased
    vector, embedder = embed(clean, api_key)
    made = Memory(doctor=doctor, kind=kind, concept=concept.strip()[:200],
                  content=clean, vector=vector, embedder=embedder,
                  created=_now(), last_used=_now())
    memories.append(made)
    save(memories, tenant)
    storage.log("memory.saved", doctor, f"{kind}: {concept[:80]}")
    return made


def forget(doctor: str, content: str, tenant: str | None = None) -> bool:
    memories = load(tenant)
    norm = content.strip().rstrip(".").lower()
    kept = [m for m in memories
            if not (m.doctor == doctor
                    and m.content.strip().rstrip(".").lower() == norm)]
    if len(kept) == len(memories):
        return False
    save(kept, tenant)
    return True


def _age_days(memory: Memory) -> float:
    stamp = memory.last_used or memory.created
    if not stamp:
        return 0.0
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return 0.0
    return max(0.0, (datetime.now() - then).total_seconds() / 86400.0)


def retrieve(doctor: str, query: str, *, kind: str | None = None, k: int = 5,
             api_key: str = "", tenant: str | None = None,
             min_score: float = 0.05) -> list[tuple[Memory, float]]:
    """
    The k most relevant memories for this doctor and this text.

    score = similarity x e^(-lambda * age_days) x confidence.
    Similarity is cosine when the query and the memory share an embedder,
    keyword overlap otherwise - never a comparison across vector spaces.
    """
    memories = [m for m in load(tenant)
                if m.doctor == doctor and (kind is None or m.kind == kind)]
    if not memories or not query.strip():
        return []
    query_vec, query_embedder = embed(query, api_key)

    scored: list[tuple[Memory, float]] = []
    for m in memories:
        if m.embedder == query_embedder and m.vector:
            similarity = cosine(query_vec, m.vector)
        else:
            similarity = _keyword_overlap(query, m.content)
        score = similarity * math.exp(-DECAY_LAMBDA * _age_days(m)) * m.confidence
        if score >= min_score:
            scored.append((m, round(score, 4)))
    scored.sort(key=lambda pair: (-pair[1], pair[0].content.lower()))
    top = scored[:k]

    if top:
        # Retrieval is reinforcement: what keeps being useful stays fresh.
        everything = load(tenant)
        by_key = {(m.doctor, m.kind, m.content): m for m in everything}
        for m, _ in top:
            hit = by_key.get((m.doctor, m.kind, m.content))
            if hit is not None:
                hit.uses += 1
                hit.last_used = _now()
        save(everything, tenant)
    return top


def context_block(doctor: str, raw_input: str, *, api_key: str = "",
                  tenant: str | None = None) -> str:
    """
    The prompt block for drafting: relevant learned rules first, then how
    this doctor handled similar past cases. Empty string when nothing is
    relevant - the prompt stays byte-identical.
    """
    rules = retrieve(doctor, raw_input, kind="style_rule", k=5,
                     api_key=api_key, tenant=tenant)
    cases = retrieve(doctor, raw_input, kind="episodic_case", k=2,
                     api_key=api_key, tenant=tenant)
    parts: list[str] = []
    if rules:
        parts.append(
            "Remembered preferences of this radiologist relevant to these "
            "notes (most relevant first):")
        parts.extend(f"- {m.content}" for m, _ in rules)
    if cases:
        parts.append(
            "\nHow this radiologist handled similar past cases - match the "
            "voice, never copy the findings:")
        for m, _ in cases:
            parts.append(f"--- PAST CASE ({m.concept or 'unlabelled'}) ---\n"
                         f"{m.content}")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Preference pairs - the DPO dataset
# --------------------------------------------------------------------------- #


def record_pair(doctor: str, raw_prompt: str, ai_draft: str, final_edit: str,
                rule: str = "", tenant: str | None = None) -> None:
    """
    Archive one (notes, AI draft, doctor's final) triple. Uncapped in spirit,
    capped at MAX_PAIRS most recent in practice - a fine-tuning dataset, not
    a landfill. No-op when the doctor changed nothing.
    """
    if ai_draft.strip() == final_edit.strip():
        return
    scope = tenant or storage.current_tenant()
    store = storage.get_store()
    try:
        payload = store.load_all(scope).get(PAIRS_NAME) or {}
    except Exception:
        payload = {}
    pairs = payload.get("pairs") if isinstance(payload, dict) else []
    if not isinstance(pairs, list):
        pairs = []
    pairs.append({
        "doctor": doctor,
        "raw_prompt": raw_prompt.strip()[:MAX_CONTENT_CHARS],
        "ai_draft": ai_draft.strip()[:MAX_CONTENT_CHARS],
        "final_edit": final_edit.strip()[:MAX_CONTENT_CHARS],
        "rule": rule.strip()[:500],
        "when": _now(),
    })
    store.save(scope, PAIRS_NAME, {
        "name": PAIRS_NAME, "kind": "preference_pairs",
        "updated": _now(), "pairs": pairs[-MAX_PAIRS:],
    })
    storage.log("preference.recorded", doctor, "draft edited and archived")


def load_pairs(tenant: str | None = None, doctor: str | None = None) -> list[dict]:
    scope = tenant or storage.current_tenant()
    try:
        payload = storage.get_store().load_all(scope).get(PAIRS_NAME) or {}
    except Exception:
        return []
    pairs = payload.get("pairs") if isinstance(payload, dict) else []
    if not isinstance(pairs, list):
        return []
    out = [p for p in pairs if isinstance(p, dict)]
    if doctor:
        out = [p for p in out if p.get("doctor") == doctor]
    return out[-MAX_PAIRS:]


def export_pairs_jsonl(tenant: str | None = None,
                       doctor: str | None = None) -> str:
    """The DPO dataset as JSONL - one training triple per line."""
    lines = [json.dumps({
        "prompt": p.get("raw_prompt", ""),
        "rejected": p.get("ai_draft", ""),
        "chosen": p.get("final_edit", ""),
        "rule": p.get("rule", ""),
        "doctor": p.get("doctor", ""),
        "when": p.get("when", ""),
    }, ensure_ascii=False) for p in load_pairs(tenant, doctor)]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Chat history - EVERY generation, not just the edited ones
# --------------------------------------------------------------------------- #


def record_chat(doctor: str, prompt: str, draft: str, section: str = "",
                tenant: str | None = None) -> None:
    """
    One drafting exchange: the prompt as given, the draft as returned. The
    preference-pairs archive only sees drafts the doctor corrected; this
    sees them all, so the history reads like the session actually went.
    """
    if not prompt.strip() or not draft.strip():
        return
    scope = tenant or storage.current_tenant()
    store = storage.get_store()
    try:
        payload = store.load_all(scope).get(CHATS_NAME) or {}
    except Exception:
        payload = {}
    chats = payload.get("chats") if isinstance(payload, dict) else []
    if not isinstance(chats, list):
        chats = []
    chats.append({
        "doctor": doctor,
        "prompt": prompt.strip()[:MAX_CONTENT_CHARS],
        "draft": draft.strip()[:MAX_CONTENT_CHARS],
        "section": section.strip()[:120],
        "when": _now(),
    })
    store.save(scope, CHATS_NAME, {
        "name": CHATS_NAME, "kind": "chat_history",
        "updated": _now(), "chats": chats[-MAX_CHATS:],
    })


def load_chats(tenant: str | None = None, doctor: str | None = None,
               limit: int = 50) -> list[dict]:
    """Newest last. Filtered to one doctor when asked."""
    scope = tenant or storage.current_tenant()
    try:
        payload = storage.get_store().load_all(scope).get(CHATS_NAME) or {}
    except Exception:
        return []
    chats = payload.get("chats") if isinstance(payload, dict) else []
    if not isinstance(chats, list):
        return []
    out = [c for c in chats if isinstance(c, dict)]
    if doctor:
        out = [c for c in out if c.get("doctor") == doctor]
    return out[-limit:]


def _rewrite_payload(name: str, list_key: str, rewrite,
                     tenant: str | None = None) -> int:
    """
    Apply `rewrite(entry)` to one reserved payload's list.

    rewrite returns None to keep an entry it MUTATED in place, True to keep
    it untouched, or False to drop it. Returns how many entries were changed
    (mutated or dropped), and persists only when something actually changed.
    """
    scope = tenant or storage.current_tenant()
    store = storage.get_store()
    try:
        payload = store.load_all(scope).get(name) or {}
    except Exception:
        return 0
    entries = payload.get(list_key) if isinstance(payload, dict) else []
    if not isinstance(entries, list):
        return 0
    kept, changed = [], 0
    for entry in entries:
        if isinstance(entry, dict):
            keep = rewrite(entry)
            if keep is None:      # mutated in place, keep it
                changed += 1
                kept.append(entry)
            elif keep is False:   # dropped
                changed += 1
            else:                 # unchanged, keep it
                kept.append(entry)
        else:
            kept.append(entry)
    if changed:
        payload = dict(payload) if isinstance(payload, dict) else {}
        payload.update({"name": name, "updated": _now(), list_key: kept})
        store.save(scope, name, payload)
    return changed


def rename_doctor(old: str, new: str, tenant: str | None = None) -> int:
    """
    A doctor's rename carries their WHOLE history: memories, preference
    pairs and chats all move to the new name. Returns how many records moved.
    """
    if not old.strip() or not new.strip() or old == new:
        return 0
    moved = 0
    memories = load(tenant)
    for m in memories:
        if m.doctor == old:
            m.doctor = new
            moved += 1
    if moved:
        save(memories, tenant)

    def _move(entry: dict):
        if entry.get("doctor") == old:
            entry["doctor"] = new
            return None   # touched, keep
        return True

    moved += _rewrite_payload(PAIRS_NAME, "pairs", _move, tenant)
    moved += _rewrite_payload(CHATS_NAME, "chats", _move, tenant)
    if moved:
        storage.log("memory.renamed", new, f"was {old}: {moved} record(s) moved")
    return moved


def forget_doctor(doctor: str, tenant: str | None = None) -> int:
    """
    Deleting a profile deletes its learning: memories, pairs and chats all
    go with it. Returns how many records were removed.
    """
    if not doctor.strip():
        return 0
    memories = load(tenant)
    kept = [m for m in memories if m.doctor != doctor]
    removed = len(memories) - len(kept)
    if removed:
        save(kept, tenant)

    def _drop(entry: dict):
        return entry.get("doctor") != doctor

    removed += _rewrite_payload(PAIRS_NAME, "pairs", _drop, tenant)
    removed += _rewrite_payload(CHATS_NAME, "chats", _drop, tenant)
    if removed:
        storage.log("memory.forgotten", doctor, f"{removed} record(s) removed")
    return removed


def profile_stats(doctor: str, tenant: str | None = None) -> dict:
    """One profile's learning at a glance - for the profile manager."""
    mine = [m for m in load(tenant) if m.doctor == doctor]
    return {
        "rules": sum(1 for m in mine if m.kind == "style_rule"),
        "cases": sum(1 for m in mine if m.kind == "episodic_case"),
        "pairs": len(load_pairs(tenant, doctor)),
        "chats": len(load_chats(tenant, doctor, limit=MAX_CHATS)),
    }
