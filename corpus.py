"""
The clinic library: an offline medical vocabulary corpus.

Doctors' books and research papers carry the exact vocabulary this clinic
dictates. Upload them once, and their terms are extracted OFFLINE - no key, no
network, deterministic - into one shared per-tenant corpus that every path can
draw on: the rule-based dictation cleanup, STT keyword biasing, and the AI
prompts.

The corpus is deliberately separate from Template.vocabulary. That list stays
the doctor's curated hot list, capped at 300 and always sent whole; the corpus
is the reference shelf behind it - tens of thousands of terms, never sent
whole anywhere. A term moves from the shelf to the hot list only by an
explicit click.

Everything here is suggest-only. Extraction shows a preview the user ticks
before anything is saved; suggestions never rewrite text; the AI is offered
correct spellings, not new findings.

Persistence rides the existing pluggable store as a payload under the reserved
name "__clinic_corpus__" - tenant isolation, optimistic concurrency and
FileStore backups all come with it, and templates.load_all() skips the name so
the record can never masquerade as a doctor's template.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

import storage
from dictation_fix import RADLEX_CORE, Suggestion, sound_key

# The store name the corpus lives under. Never a template: templates.load_all
# skips exactly this string, and the new-template form refuses it.
RESERVED_NAME = "__clinic_corpus__"

# Caps, enforced on every load like templates.from_dict enforces its own.
# A corpus is bounded so a hostile or runaway import cannot balloon storage.
MAX_CORPUS_TERMS = 50_000
MAX_SOURCES = 200
MAX_TERM_CHARS = 60
MIN_TERM_CHARS = 4

# Reference chunks: whole passages kept for retrieval WITH citation, so an
# answer can say which book and which part it came from.
CHUNK_CHARS = 1400
MAX_CHUNKS_PER_SOURCE = 300
MAX_CHUNKS_TOTAL = 2000

# --------------------------------------------------------------------------- #
# What a corpus is
# --------------------------------------------------------------------------- #


@dataclass
class CorpusTerm:
    term: str                                   # canonical spelling, first seen
    count: int = 0                              # occurrences across all sources
    sources: dict[str, int] = field(default_factory=dict)   # filename -> count


@dataclass
class Corpus:
    terms: dict[str, CorpusTerm] = field(default_factory=dict)  # keyed lowercase
    sources: dict[str, dict] = field(default_factory=dict)      # filename -> meta
    chunks: list[dict] = field(default_factory=list)  # {source, seq, text, vector, embedder}
    updated: str = ""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Extraction - offline and deterministic
# --------------------------------------------------------------------------- #

# Common English plus report/academic filler. This list protects the
# "rare long word" branch below from swallowing ordinary prose; the medical
# suffix/prefix branches barely need it. Deliberately plain lowercase ASCII.
COMMON_ENGLISH = frozenset("""
a about above across after again against all almost alone along already also
although always am among amount an and another any anybody anyone anything
anywhere are area areas around as ask asked asking asks at away back backed
backing backs be became because become becomes been before began behind being
beings below best better between beyond big both but by came can cannot case
cases certain certainly clear clearly come could did differ different
differently do does done down downed downing downs during each early either
end ended ending ends enough even evenly ever every everybody everyone
everything everywhere face faces fact facts far felt few find finds first for
four from full fully further furthered furthering furthers gave general
generally get gets give given gives go going good goods got great greater
greatest group grouped grouping groups had has have having he her here herself
high higher highest him himself his how however if important in interest
interested interesting interests into is it its itself just keep keeps kind
knew know known knows large largely last later latest least less let lets
like likely long longer longest made make making man many may me member
members men might more most mostly mr mrs much must my myself necessary need
needed needing needs never new newer newest next no nobody non noone not
nothing now nowhere number numbers of off often old older oldest on once one
only open opened opening opens or order ordered ordering orders other others
our out over part parted parting parts per perhaps place places point pointed
pointing points possible present presented presenting presents problem
problems put puts quite rather really right room rooms said same saw say says
second seconds see seem seemed seeming seems sees several shall she should
show showed showing shows side sides since small smaller smallest so some
somebody someone something somewhere state states still such sure take taken
than that the their them then there therefore these they thing things think
thinks this those though thought thoughts three through thus to today
together too took toward turn turned turning turns two under until up upon us
use used uses very want wanted wanting wants was way ways we well wells went
were what when where whether which while who whole whose why will with within
without work worked working works would year years yet you young younger
youngest your yours
above abstract according accordingly acknowledge addition additional
additionally address adequate advance advantage affect afterwards agree
agreement allow allowed almost already alternative although analysis analyses
analyzed apparent apparently appear appeared appears application applied
applies apply approach appropriate approximate approximately argue argument
article aspects assess assessed assessment associate associated association
assume assumed assumption attempt attention author authors available average
background baseline basic basis becomes beginning behaviour behavior believe
benefit benefits calculate calculated calculation category chapter
characteristic characteristics classification classified clinical collection
combination combined common commonly compare compared comparison complete
completely complex component components composition comprehensive concept
concern concerning conclude concluded conclusion conclusions condition
conditions conduct conducted conference confidence confirm confirmed consider
considerable considerably consideration considered consistent consistently
constant construct contain contained containing contains content context
continue continued continuous contrast contribute contribution control
controlled convention conventional correlate correlated correlation correspond
corresponding criteria criterion current currently database decrease decreased
define defined definition degree demonstrate demonstrated demonstrates
department depend dependent depending depends describe described describes
description design designed detail detailed details detect detected detection
determine determined develop developed developing development difference
differences difficult dimension direction discuss discussed discussion
disease diseases distribution document documented easily edition editor
education effect effective effectively effects efficiency efficient element
elements emphasis employed enable environment equal equally equipment
equivalent especially essential establish established estimate estimated
evaluate evaluated evaluation event events evidence evident exact exactly
examination examine examined example examples excellent except exception
exclude excluded exclusion exhibit exist existing exists expect expected
experience experiment experimental explain explained explanation express
expressed expression extension extensive extent external factor factors
failure feature features figure figures final finally finding findings
focus focused following follows formation frequency frequent frequently
function functional functions fundamental furthermore generally generate
generated give greatly guidelines highly history hypothesis identified
identify illustrate illustrated image images imaging immediate immediately
impact implement implication importance important improve improved improvement
incidence include included includes including increase increased increasing
independent index indicate indicated indicates individual individuals
influence information initial initially institute institution instrument
interaction internal international interpretation interval introduce
introduced introduction investigate investigated investigation involve
involved involvement involves journal knowledge laboratory language largely
leading level levels limitation limitations limited literature located
location magnitude maintain maintained majority management material materials
maximum measure measured measurement measurements mechanism mechanisms
medical medicine method methods minimal minimum moderate moreover multiple
national natural nature necessary negative normal normally noted notes
objective observation observations observe observed obtain obtained obvious
obviously occur occurred occurrence occurring occurs operation original
outcome outcomes overall paper parameter parameters partial partially
participants particular particularly patient patients pattern patterns
percent percentage perform performance performed period physical population
position positive potential potentially practice precise precisely predict
predicted prediction preliminary presence previous previously primary
principle probability probable probably procedure procedures process
processes produce produced product professor prominent properties property
proportion propose proposed protocol provide provided provides publication
published purpose quality quantitative quantity question radiology random
range rarely rates ratio reason reasons recent recently recognize recognized
recommend recommended reduce reduced reduction reference references regard
regarding region regions relate related relation relationship relative
relatively relevant reliable remain remained remaining remains report
reported reports represent representative represented request require
required requirement requires research resolution respect respective
respectively response result resulted resulting results review reviewed
sample samples science scientific section sections select selected selection
sensitive separate sequence series service session severe severity
significance significant significantly similar similarly simple simply
single situation slightly society software solution source sources specific
specifically standard statistical statistically strategy strength stress
strong strongly structure structures studied studies study subject subjects
subsequent substantial substantially sufficient suggest suggested suggests
summary supply support supported surface survey system systematic systems
table technique techniques technology temperature term terms theory
therefore threshold throughout total training treatment trial trials
typical typically understand understanding underwent uniform unique
university unknown unlikely usually validity value values variable variables
variation various version visible volume weight widely
""".split())

# Greco-latinate word shapes that mark a medical term. Suffixes checked on the
# lowercase word (and with a trailing "s" stripped); prefixes are the strongly
# anatomical ones only - generic latin prefixes (peri-, sub-, trans-) drag in
# ordinary English, and the rare-long-word branch catches those terms anyway.
_MED_SUFFIXES = (
    "itis", "osis", "oses", "iasis", "oma", "omas", "omata",
    "ectomy", "otomy", "ostomy", "plasty", "graphy", "gram", "scopy",
    "scopic", "pathy", "pathic", "megaly", "penia", "emia", "aemia",
    "uria", "cele", "lysis", "lytic", "stenosis", "sclerosis", "plasia",
    "trophy", "trophic", "ectasia", "ectasis", "ptosis", "rrhage",
    "rrhagic", "rrhea", "rrhoea", "algia", "edema", "oedema", "genic",
    "genesis", "roid", "echoic", "centesis", "pnea", "pnoea",
)
_MED_PREFIXES = (
    "chole", "nephro", "hepato", "spleno", "pneumo", "hydro", "osteo",
    "chondro", "arthro", "cardio", "broncho", "gastro", "entero",
    "hystero", "oophor", "salping", "encephalo", "myelo", "angio",
    "arterio", "adeno", "cranio", "thoraco", "pleuro", "pericard",
    "endometri", "lympho", "haemato", "hemato", "musculo", "spondylo",
)

_TOKEN = re.compile(r"[A-Za-z][A-Za-z'\-]{3,}")


def looks_medical(word: str) -> str:
    """
    Why this word looks like a medical term - or "" when it does not.

    The reason string is shown in the extraction preview, because a user
    deciding whether to keep a term deserves to know why it was offered.
    """
    lower = word.lower().strip("'-")
    if not lower or lower in COMMON_ENGLISH:
        return ""
    if not (MIN_TERM_CHARS <= len(lower) <= MAX_TERM_CHARS):
        return ""
    stem = lower[:-1] if lower.endswith("s") and not lower.endswith("ss") else lower
    for suffix in _MED_SUFFIXES:
        if lower.endswith(suffix) or stem.endswith(suffix):
            return f"suffix -{suffix}"
    for prefix in _MED_PREFIXES:
        if lower.startswith(prefix):
            return f"prefix {prefix}-"
    if len(lower) >= 10 and lower.isalpha():
        return "rare long word"
    return ""


def extract_terms(text: str, *, source: str = "", min_count: int = 2,
                  phrase_min_count: int = 3, known: set[str] | None = None,
                  max_terms: int = 5000) -> list[CorpusTerm]:
    """
    Candidate medical terms from a book chapter or paper. Pure and
    deterministic: the same text always yields the same list, in the same
    order. `known` (lowercase) is skipped - terms the clinic already has.
    """
    skip = {k.lower() for k in (known or set())}
    tokens = _TOKEN.findall(text or "")

    singles: Counter[str] = Counter()
    casing: dict[str, str] = {}
    for token in tokens:
        lower = token.lower().strip("'-")
        if not lower or lower in skip:
            continue
        singles[lower] += 1
        # Prefer the lowercase spelling when both appear: sentence-initial
        # capitals are an artefact, not the canonical form.
        if lower not in casing or (token == lower and casing[lower] != lower):
            casing[lower] = token if token != token.capitalize() else lower

    out: dict[str, CorpusTerm] = {}
    for lower, count in singles.items():
        if count < min_count:
            continue
        if not looks_medical(lower):
            continue
        out[lower] = CorpusTerm(term=casing.get(lower, lower), count=count,
                                sources={source: count} if source else {})

    # Phrases: adjacent pairs and triples where nothing is filler and at least
    # one word is evidently medical (or long enough to be jargon).
    radlex = {t.lower() for t in RADLEX_CORE}

    def _phrase_worthy(parts: tuple[str, ...]) -> bool:
        if any(p in COMMON_ENGLISH or len(p) < MIN_TERM_CHARS for p in parts):
            return False
        return any(looks_medical(p) or p in radlex or len(p) >= 8
                   for p in parts)

    lowers = [t.lower().strip("'-") for t in tokens]
    phrases: Counter[tuple[str, ...]] = Counter()
    for n in (2, 3):
        for i in range(len(lowers) - n + 1):
            parts = tuple(lowers[i:i + n])
            if _phrase_worthy(parts):
                phrases[parts] += 1
    kept_phrases = {
        " ".join(parts): count for parts, count in phrases.items()
        if count >= phrase_min_count and " ".join(parts) not in skip
        and len(" ".join(parts)) <= MAX_TERM_CHARS
    }
    for joined, count in kept_phrases.items():
        # A bigram that only ever occurs inside a kept trigram is an artefact
        # of the window, not a term of its own.
        if any(joined != longer and joined in longer and count <= n
               for longer, n in kept_phrases.items()):
            continue
        if joined in out:
            continue
        out[joined] = CorpusTerm(term=joined, count=count,
                                 sources={source: count} if source else {})

    ranked = sorted(out.values(), key=lambda t: (-t.count, t.term.lower()))
    return ranked[:max_terms]


# --------------------------------------------------------------------------- #
# Persistence - rides the existing store
# --------------------------------------------------------------------------- #


def to_dict(corpus: Corpus) -> dict:
    return {
        "name": RESERVED_NAME,
        "kind": "corpus",
        "updated": corpus.updated or _now(),
        "sources": dict(corpus.sources),
        "terms": [
            {"term": t.term, "count": t.count, "sources": dict(t.sources)}
            for t in sorted(corpus.terms.values(),
                            key=lambda t: (-t.count, t.term.lower()))
        ],
        "chunks": list(corpus.chunks[:MAX_CHUNKS_TOTAL]),
    }


def from_dict(payload: dict) -> Corpus:
    """Hostile-input tolerant, caps enforced - like templates.from_dict."""
    corpus = Corpus()
    if not isinstance(payload, dict):
        return corpus
    corpus.updated = str(payload.get("updated", ""))

    raw_sources = payload.get("sources")
    if isinstance(raw_sources, dict):
        for name, meta in list(raw_sources.items())[:MAX_SOURCES]:
            if isinstance(name, str) and name.strip() and isinstance(meta, dict):
                corpus.sources[name.strip()] = {
                    "added_at": str(meta.get("added_at", "")),
                    "added_by": str(meta.get("added_by", "")),
                    "terms": int(meta.get("terms", 0) or 0),
                    "sha1": str(meta.get("sha1", "")),
                }

    raw_terms = payload.get("terms")
    if isinstance(raw_terms, list):
        for entry in raw_terms:
            if len(corpus.terms) >= MAX_CORPUS_TERMS:
                break
            if not isinstance(entry, dict):
                continue
            term = str(entry.get("term", "")).strip()
            if not term or len(term) > MAX_TERM_CHARS:
                continue
            try:
                count = max(0, int(entry.get("count", 0) or 0))
            except (TypeError, ValueError):
                count = 0
            sources = entry.get("sources")
            clean_sources: dict[str, int] = {}
            if isinstance(sources, dict):
                for src, n in sources.items():
                    if isinstance(src, str) and src.strip():
                        try:
                            clean_sources[src.strip()] = max(0, int(n or 0))
                        except (TypeError, ValueError):
                            continue
            corpus.terms[term.lower()] = CorpusTerm(term=term, count=count,
                                                    sources=clean_sources)

    raw_chunks = payload.get("chunks")
    if isinstance(raw_chunks, list):
        for entry in raw_chunks[:MAX_CHUNKS_TOTAL]:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text", "")).strip()
            source = str(entry.get("source", "")).strip()
            if not text or not source:
                continue
            vector = entry.get("vector")
            corpus.chunks.append({
                "source": source,
                "seq": max(1, int(entry.get("seq", 1) or 1)),
                "text": text[:CHUNK_CHARS * 2],
                "vector": [float(v) for v in vector
                           if isinstance(v, (int, float))]
                if isinstance(vector, list) else [],
                "embedder": str(entry.get("embedder", "hash-256")),
            })
    return corpus


def load(tenant: str | None = None) -> Corpus:
    """The tenant's corpus; empty (never an error) when absent or corrupt."""
    scope = tenant or storage.current_tenant()
    try:
        payload = storage.get_store().load_all(scope).get(RESERVED_NAME)
    except Exception:
        return Corpus()
    return from_dict(payload) if payload else Corpus()


def fingerprint(tenant: str | None = None) -> str:
    scope = tenant or storage.current_tenant()
    try:
        return storage.get_store().fingerprint(scope, RESERVED_NAME)
    except Exception:
        return ""


def save(corpus: Corpus, tenant: str | None = None, *,
         expect: str | None = None) -> None:
    scope = tenant or storage.current_tenant()
    corpus.updated = _now()
    storage.get_store().save(scope, RESERVED_NAME, to_dict(corpus), expect=expect)
    storage.log("corpus.saved", RESERVED_NAME,
                f"{len(corpus.terms)} term(s) from {len(corpus.sources)} source(s)")


# --------------------------------------------------------------------------- #
# Mutation - pure; the caller saves
# --------------------------------------------------------------------------- #


def add_source(corpus: Corpus, source_name: str, terms: list[CorpusTerm], *,
               added_by: str = "", sha1: str = "", full_text: str = "",
               api_key: str = "") -> Corpus:
    """
    Add (or replace) one uploaded document's terms - and, when the full text
    is offered, its reference chunks for cited retrieval.
    """
    name = source_name.strip()
    if not name:
        return corpus
    if name in corpus.sources:
        corpus = remove_source(corpus, name)   # re-upload replaces
    if len(corpus.sources) >= MAX_SOURCES:
        raise ValueError(f"The library holds at most {MAX_SOURCES} sources.")
    for incoming in terms:
        lower = incoming.term.lower()
        count = sum(incoming.sources.values()) or incoming.count
        existing = corpus.terms.get(lower)
        if existing is None:
            corpus.terms[lower] = CorpusTerm(term=incoming.term, count=count,
                                             sources={name: count})
        else:
            existing.count += count
            existing.sources[name] = existing.sources.get(name, 0) + count
    if full_text.strip():
        import memory as memory_engine

        for seq, piece in enumerate(chunk_text(full_text), start=1):
            if len(corpus.chunks) >= MAX_CHUNKS_TOTAL:
                break
            vector, embedder = memory_engine.embed(piece, api_key)
            corpus.chunks.append({"source": name, "seq": seq, "text": piece,
                                  "vector": vector, "embedder": embedder})
    corpus.sources[name] = {"added_at": _now(), "added_by": added_by,
                            "terms": len(terms), "sha1": sha1,
                            "chunks": sum(1 for c in corpus.chunks
                                          if c.get("source") == name)}
    return corpus


def remove_source(corpus: Corpus, source_name: str) -> Corpus:
    """Take one document out; terms it alone contributed disappear."""
    name = source_name.strip()
    corpus.sources.pop(name, None)
    for lower in list(corpus.terms):
        term = corpus.terms[lower]
        contributed = term.sources.pop(name, 0)
        term.count = max(0, term.count - contributed)
        if not term.sources or term.count <= 0:
            del corpus.terms[lower]
    corpus.chunks = [c for c in corpus.chunks if c.get("source") != name]
    return corpus


def merge(corpus: Corpus, other: Corpus) -> Corpus:
    """Fold an imported corpus in. Existing sources with the same name win."""
    for name, meta in other.sources.items():
        if name in corpus.sources or len(corpus.sources) >= MAX_SOURCES:
            continue
        corpus.sources[name] = dict(meta)
        for chunk in other.chunks:
            if chunk.get("source") == name \
                    and len(corpus.chunks) < MAX_CHUNKS_TOTAL:
                corpus.chunks.append(dict(chunk))
        for lower, incoming in other.terms.items():
            contributed = incoming.sources.get(name, 0)
            if not contributed:
                continue
            existing = corpus.terms.get(lower)
            if existing is None:
                if len(corpus.terms) >= MAX_CORPUS_TERMS:
                    continue
                corpus.terms[lower] = CorpusTerm(term=incoming.term, count=contributed,
                                                 sources={name: contributed})
            else:
                existing.count += contributed
                existing.sources[name] = existing.sources.get(name, 0) + contributed
    return corpus


# --------------------------------------------------------------------------- #
# The index - near-miss lookup that stays fast at fifty thousand terms
# --------------------------------------------------------------------------- #


class TermIndex:
    """
    Bucketed lookup. dictation_fix.near_misses runs difflib over the WHOLE
    vocabulary per token - right at 300 hot-list terms, fatal at 50 000. Here
    every probe touches only a shortlist: the exact sound_key bucket, plus the
    (first-sound-letter, length band) shape buckets either side.
    """

    def __init__(self, terms, counts: dict[str, int] | None = None) -> None:
        self._canon: dict[str, str] = {}         # lowercase -> canonical
        self._counts: dict[str, int] = {}
        self._by_sound: dict[str, list[str]] = {}
        self._by_shape: dict[tuple[str, int], list[str]] = {}
        counts = counts or {}
        for raw in terms:
            term = str(raw).strip()
            if not term:
                continue
            lower = term.lower()
            if lower in self._canon:
                continue
            self._canon[lower] = term
            self._counts[lower] = int(counts.get(lower, counts.get(term, 0)) or 0)
            key = sound_key(lower.replace(" ", ""))
            if key:
                self._by_sound.setdefault(key, []).append(lower)
                shape = (key[0], len(lower) // 3)
                self._by_shape.setdefault(shape, []).append(lower)
        for bucket in self._by_sound.values():
            bucket.sort(key=lambda w: (-self._counts.get(w, 0), w))
        for bucket in self._by_shape.values():
            bucket.sort(key=lambda w: (-self._counts.get(w, 0), w))

    def __contains__(self, term: str) -> bool:
        return str(term).strip().lower() in self._canon

    def __len__(self) -> int:
        return len(self._canon)

    def canonical(self, term: str) -> str:
        return self._canon.get(str(term).strip().lower(), "")

    def count(self, term: str) -> int:
        return self._counts.get(str(term).strip().lower(), 0)

    def top(self, k: int) -> list[str]:
        ranked = sorted(self._canon,
                        key=lambda w: (-self._counts.get(w, 0), w))
        return [self._canon[w] for w in ranked[:k]]

    def candidates(self, token: str) -> list[str]:
        """A shortlist worth running difflib on - never the whole corpus."""
        lower = str(token).strip().lower()
        key = sound_key(lower.replace(" ", ""))
        if not key:
            return []
        cap = 80
        seen: set[str] = set()
        out: list[str] = []

        def _take(words) -> bool:
            for word in words:
                if len(out) >= cap:
                    return True
                if word not in seen:
                    seen.add(word)
                    out.append(word)
            return False

        # Buckets are sorted heaviest-first, so a capped walk still sees the
        # terms most likely to be dictated.
        if not _take(self._by_sound.get(key, ())):
            band = len(lower) // 3
            for shape in ((key[0], band), (key[0], band - 1),
                          (key[0], band + 1)):
                if _take(self._by_shape.get(shape, ())):
                    break
        return out

    def near(self, token: str, threshold: float = 0.78):
        """
        The closest corpus term, as (canonical, score, reason) - or None.
        reason mirrors dictation_fix: "spelling" | "sound".
        """
        lower = str(token).strip().lower()
        if not lower or lower in self._canon:
            return None
        key = sound_key(lower.replace(" ", ""))

        best: tuple[str, float, str] | None = None
        for word in self.candidates(lower):
            # Cheap upper bounds first - ratio() is the expensive call, and on
            # a shortlist of lookalikes it dominates the whole pass.
            bound = 2 * min(len(lower), len(word)) / (len(lower) + len(word))
            if bound < threshold:
                continue
            matcher = difflib.SequenceMatcher(None, lower, word)
            if matcher.real_quick_ratio() < threshold \
                    or matcher.quick_ratio() < threshold:
                continue
            score = matcher.ratio()
            if score >= threshold and score < 0.99:
                if best is None or score > best[1]:
                    best = (self._canon[word], round(score, 2), "spelling")
        if best is not None:
            return best

        # Identical sound skeleton: strong signal even when the letters differ.
        if key and len(key) >= 3:
            for word in self._by_sound.get(key, ()):
                if word != lower:
                    return (self._canon[word], 0.9, "sound")
        return None


# Process-level cache, keyed (tenant, store fingerprint) - correct under both
# Streamlit and uvicorn without depending on either's caching.
_index_cache: dict[str, tuple[str, TermIndex]] = {}


def get_index(tenant: str | None = None) -> TermIndex:
    scope = tenant or storage.current_tenant()
    print_ = fingerprint(scope)
    cached = _index_cache.get(scope)
    if cached and cached[0] == print_:
        return cached[1]
    corpus = load(scope)
    index = TermIndex((t.term for t in corpus.terms.values()),
                      {lower: t.count for lower, t in corpus.terms.items()})
    _index_cache[scope] = (print_, index)
    return index


# --------------------------------------------------------------------------- #
# Suggestions - a separate pass AFTER dictation_fix.clean, merged by callers
# --------------------------------------------------------------------------- #


def suggest(text: str, index: TermIndex,
            exclude: set[str] | None = None) -> list[Suggestion]:
    """
    Corpus near-misses in the text. Suggestion objects only - there is no
    code path here that returns text, so auto-applying is impossible.
    """
    if not text or not len(index):
        return []
    skip = {str(e).strip().lower() for e in (exclude or set())}
    tokens = _TOKEN.findall(text)
    singles = [t for t in tokens if t.lower() not in skip]
    pairs = [f"{a} {b}" for a, b in zip(tokens, tokens[1:])
             if a.lower() not in index._canon and b.lower() not in index._canon]

    out: dict[str, Suggestion] = {}
    for candidate in dict.fromkeys(singles + pairs):   # ordered, deduped
        lower = candidate.lower()
        if lower in skip or lower in index._canon:
            continue
        found = index.near(candidate)
        if found is None:
            continue
        suggested, score, _why = found
        if suggested.lower() == lower:
            continue
        out[candidate] = Suggestion(candidate, suggested, score, "corpus")
    ranked = sorted(out.values(), key=lambda s: (-s.confidence, s.heard.lower()))
    return ranked[:20]


# --------------------------------------------------------------------------- #
# Top-K selection for prompts - the whole corpus never enters one
# --------------------------------------------------------------------------- #


def relevant_terms(text: str, index: TermIndex, k: int = 40) -> list[str]:
    """
    The corpus terms that matter for THIS text: exactly present (reinforce the
    correct spelling) or phonetically near a token that is present.
    """
    if not text or not len(index) or k <= 0:
        return []
    tokens = [t.lower() for t in _TOKEN.findall(text)]
    tokens = [t for t in tokens if t not in COMMON_ENGLISH]
    candidates = list(dict.fromkeys(
        tokens
        + [f"{a} {b}" for a, b in zip(tokens, tokens[1:])]
        + [f"{a} {b} {c}" for a, b, c in zip(tokens, tokens[1:], tokens[2:])]))

    scored: dict[str, tuple[float, int]] = {}
    for candidate in candidates:
        if candidate in index:
            canonical = index.canonical(candidate)
            scored[canonical] = max(scored.get(canonical, (0, 0)),
                                    (1.0, index.count(candidate)))
            continue
        found = index.near(candidate, threshold=0.75)
        if found:
            canonical, score, _why = found
            entry = (score, index.count(canonical))
            if entry > scored.get(canonical, (0, 0)):
                scored[canonical] = entry
    ranked = sorted(scored.items(),
                    key=lambda kv: (-kv[1][0], -kv[1][1], kv[0].lower()))
    return [term for term, _ in ranked[:k]]


def stt_hint(context: str, hot_vocabulary: list[str], index: TermIndex,
             max_chars: int = 800) -> str:
    """
    A keyword string for STT biasing (the Whisper `prompt` field). Built
    BEFORE any transcript exists, so it leans on the study context and the
    doctor's hot list first, then the library's context matches, then its
    heaviest terms. Hard-capped: Whisper reads ~224 tokens of prompt.
    """
    parts: list[str] = []
    seen: set[str] = set()

    def _take(term: str) -> None:
        clean = str(term).strip()
        if clean and clean.lower() not in seen:
            seen.add(clean.lower())
            parts.append(clean)

    for term in hot_vocabulary or []:
        _take(term)
    for term in relevant_terms(context or "", index, k=20):
        _take(term)
    for term in index.top(30):
        _take(term)

    out = ""
    for part in parts:
        joined = f"{out}, {part}" if out else part
        if len(joined) > max_chars:
            break
        out = joined
    return out


# --------------------------------------------------------------------------- #
# Reference chunks - retrieval with a citation, never an uncited answer
# --------------------------------------------------------------------------- #


def chunk_text(text: str) -> list[str]:
    """
    Section-aware pieces of a document: paragraphs packed to ~CHUNK_CHARS,
    with the previous paragraph carried over so a sentence split across a
    boundary is still findable. Deterministic.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text or "")
                  if p.strip()]
    chunks: list[str] = []
    current = ""
    previous = ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) + 2 > CHUNK_CHARS:
            chunks.append(current)
            # Overlap: the last paragraph rides into the next chunk.
            current = f"{previous}\n\n{paragraph}" if previous else paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
        previous = paragraph
        if len(chunks) >= MAX_CHUNKS_PER_SOURCE:
            break
    if current and len(chunks) < MAX_CHUNKS_PER_SOURCE:
        chunks.append(current)
    return chunks


def search_chunks(query: str, k: int = 3, tenant: str | None = None,
                  api_key: str = "") -> list[dict]:
    """
    The k most relevant passages across every uploaded document, each with
    its citation: {"citation": "source · part N", "text": ..., "score": ...}.
    """
    import memory as memory_engine

    library = load(tenant)
    if not library.chunks or not query.strip():
        return []
    query_vec, query_embedder = memory_engine.embed(query, api_key)

    scored = []
    for chunk in library.chunks:
        if chunk.get("embedder") == query_embedder and chunk.get("vector"):
            score = memory_engine.cosine(query_vec, chunk["vector"])
        else:
            score = memory_engine._keyword_overlap(query, chunk.get("text", ""))
        if score > 0.02:
            scored.append((score, chunk))
    scored.sort(key=lambda pair: (-pair[0], pair[1].get("source", ""),
                                  pair[1].get("seq", 0)))
    return [{
        "citation": f"{c.get('source', '?')} · part {c.get('seq', '?')}",
        "text": c.get("text", ""),
        "score": round(s, 3),
    } for s, c in scored[:k]]
