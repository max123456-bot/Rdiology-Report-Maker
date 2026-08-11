"""
Deterministic cleanup applied after speech-to-text.

The model does most of the work. These are the residual errors it still makes,
caught with rules rather than another API call - so they cost nothing, cannot
themselves hallucinate, and always behave the same way.

Three passes, in order:

  spoken_numbers()   "one point four centimetres" -> "1.4 cm". Every engine
                     leaves some of these, and AI4Bharat leaves nearly all of
                     them because it only transcribes words.

  units()            "4mm" -> "4 mm", "4 MM" -> "4 mm". Consistency, so the
                     validator can compare a measurement in the impression with
                     the same one in the findings.

  near_misses()      Words that are almost a term this doctor uses. Reported,
                     never applied silently - "colic list" for "cholelithiasis"
                     is a suggestion for the radiologist, not a correction to
                     make on their behalf.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Spoken numbers
# --------------------------------------------------------------------------- #

_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
# "lakh" included because an Indian radiologist dictating a cell count says it.
_SCALES = {"hundred": 100, "thousand": 1000, "lakh": 100_000}

# How radiologists say units out loud, mapped to how they must be written.
UNIT_WORDS = {
    "millimetre": "mm", "millimetres": "mm", "millimeter": "mm", "millimeters": "mm",
    "milimeter": "mm", "milimeters": "mm", "mm": "mm",
    "centimetre": "cm", "centimetres": "cm", "centimeter": "cm", "centimeters": "cm",
    "centimetre s": "cm", "cm": "cm",
    "millilitre": "ml", "millilitres": "ml", "milliliter": "ml", "milliliters": "ml",
    "ml": "ml", "cc": "cc",
    "percent": "%", "per cent": "%", "percentage": "%",
    "hounsfield": "HU", "hounsfield units": "HU",
    "megahertz": "MHz",
}

# Deliberately NOT units: "week" and "year". Normalising them pluralised
# "seventy year old male" into "70 years old male", corrupting the grammar of
# the commonest phrase in a radiology report. The number still converts; the
# word is left exactly as dictated.

_NUMBER_WORD = "|".join(
    sorted(list(_ONES) + list(_TENS) + list(_SCALES) + ["point", "and"],
           key=len, reverse=True)
)
_SPOKEN = re.compile(rf"\b((?:{_NUMBER_WORD})(?:[ -](?:{_NUMBER_WORD}))*)\b", re.I)


def _words_to_number(phrase: str) -> str | None:
    """'fourteen point two' -> '14.2'. None when the phrase is not a number."""
    tokens = [t for t in re.split(r"[ -]+", phrase.lower().strip()) if t and t != "and"]
    if not tokens:
        return None

    whole_tokens, decimal_tokens, seen_point = [], [], False
    for token in tokens:
        if token == "point":
            if seen_point:
                return None  # two decimal points is not a number
            seen_point = True
            continue
        (decimal_tokens if seen_point else whole_tokens).append(token)

    def whole(parts: list[str]) -> int | None:
        """
        Standard English number grammar only.

        "twenty three" is 23. "two fifty" is NOT 52 - it is how many Indian
        English speakers say 250, and an additive parser silently turned it into
        52. A wrong measurement is worse than an unconverted one, so anything
        that does not follow tens-then-ones is left as words for the radiologist
        to write themselves.
        """
        total = current = 0
        matched = False
        previous: str | None = None

        for part in parts:
            if part in _ONES:
                # A ones-word directly after a tens-word is fine ("twenty three").
                # A ones-word directly BEFORE a tens-word is ambiguous.
                if previous in _ONES:
                    return None  # "two three" - not a number, or a digit string
                current += _ONES[part]
                matched = True
            elif part in _TENS:
                if previous in _ONES:
                    return None  # "two fifty" - ambiguous, refuse to guess
                current += _TENS[part]
                matched = True
            elif part in _SCALES:
                if current == 0:
                    current = 1
                scale = _SCALES[part]
                if scale == 100:
                    current *= scale
                else:
                    total += current * scale
                    current = 0
                matched = True
            else:
                return None
            previous = part

        return (total + current) if matched else None

    left = whole(whole_tokens) if whole_tokens else (0 if seen_point else None)
    if left is None:
        return None

    if not seen_point:
        return str(left)

    # After a decimal point each word is a single digit: "point four two" -> .42
    digits = ""
    for part in decimal_tokens:
        if part in _ONES and _ONES[part] <= 9:
            digits += str(_ONES[part])
        else:
            return None
    if not digits:
        return None
    return f"{left}.{digits}"


def spoken_numbers(text: str) -> tuple[str, int]:
    """Turn spoken numbers into figures. Returns the text and how many changed."""
    changes = 0

    def replace(match: re.Match) -> str:
        nonlocal changes
        phrase = match.group(1)

        # A lone "one"/"and"/"point" is almost always ordinary prose.
        if len(re.split(r"[ -]+", phrase.strip())) == 1 and phrase.lower() in (
            "one", "and", "point", "ten", "hundred", "thousand", "lakh"
        ):
            return phrase

        # "a hundred percent normal", "a thousand times better" - an article in
        # front means it is being used as a figure of speech, not a measurement.
        preceding = text[max(0, match.start() - 3):match.start()].lower()
        if preceding.endswith(("a ", "an ")):
            return phrase

        # "one and a half centimetres" - a fraction this parser cannot express.
        # Converting only the leading word produced "1 a half", so leave it whole
        # and let the radiologist write the fraction they meant.
        following = text[match.end():match.end() + 14].lower()
        if re.match(r"\s*(a\s+)?(half|quarter|third)\b", following):
            return phrase

        value = _words_to_number(phrase)
        if value is None:
            return phrase
        changes += 1
        return value

    return _SPOKEN.sub(replace, text), changes


def units(text: str) -> tuple[str, int]:
    """Normalise units so the same measurement is written the same way twice."""
    changes = 0

    # "14.2 centimetres" -> "14.2 cm", and "4mm" -> "4 mm".
    # No \b before the unit: there is no word boundary between "4" and "mm",
    # so requiring one silently skipped every unspaced measurement.
    words = "|".join(sorted(UNIT_WORDS, key=len, reverse=True))
    pattern = re.compile(rf"(\d+(?:\.\d+)?)\s*({words})\b", re.I)

    def replace(match: re.Match) -> str:
        nonlocal changes
        number, unit = match.group(1), match.group(2).lower()
        proper = UNIT_WORDS.get(unit, unit)
        # A percent sign sits against its number; every other unit takes a space.
        rewritten = f"{number}{proper}" if proper == "%" else f"{number} {proper}"
        if rewritten != match.group(0):
            changes += 1
        return rewritten

    text = pattern.sub(replace, text)

    # "9.8 by 4.4 cm" and "9.8 into 4.4 cm" both mean 9.8 x 4.4. "into" is how
    # dimensions are usually spoken in India.
    text, n = re.subn(r"(\d(?:\.\d+)?)\s+(?:by|into)\s+(\d)", r"\1 x \2", text, flags=re.I)
    changes += n
    return text, changes


# --------------------------------------------------------------------------- #
# Near misses against the doctor's own vocabulary
# --------------------------------------------------------------------------- #


@dataclass
class Suggestion:
    heard: str
    suggested: str
    confidence: float
    reason: str = "spelling"     # spelling | sound


# Spelling differences that sound identical, collapsed so a phonetic comparison
# sees through them. Order matters: longer patterns first.
_SOUND_RULES = [
    (re.compile(r"ph"), "f"),
    (re.compile(r"ch"), "k"),
    (re.compile(r"th"), "t"),
    (re.compile(r"sh"), "s"),
    (re.compile(r"gh"), ""),
    (re.compile(r"kn|gn|pn|wr"), "n"),
    (re.compile(r"[cq]"), "k"),
    (re.compile(r"x"), "ks"),
    (re.compile(r"z"), "s"),
    (re.compile(r"y"), "i"),
    (re.compile(r"(.)\1+"), r"\1"),   # doubled letters
]


def sound_key(word: str) -> str:
    """
    A rough phonetic skeleton, for catching what edit distance misses.

    A speech engine writing "hydro nephrosis" or "coleli thiasis" produces
    something that looks unlike the real term letter by letter but sounds almost
    identical. Collapsing the spellings that sound the same, then dropping the
    vowels, makes those line up.

    Not a full Metaphone - deliberately. This only has to be good enough to
    shortlist a candidate the radiologist then confirms.
    """
    key = re.sub(r"[^a-z]", "", (word or "").lower())
    if not key:
        return ""
    for pattern, replacement in _SOUND_RULES:
        key = pattern.sub(replacement, key)
    if not key:
        return ""
    # Keep the first letter, drop the rest of the vowels: consonant shape is
    # what survives a mis-hearing.
    return key[0] + re.sub(r"[aeiou]", "", key[1:])


def near_misses(text: str, vocabulary: list[str], threshold: float = 0.78) -> list[Suggestion]:
    """
    Words close to a term this doctor uses, but not quite it.

    Only ever suggested. Silently rewriting a medical term because it looked
    similar is exactly the kind of "helpful" behaviour that puts a wrong word in
    a report, so the radiologist decides.
    """
    if not vocabulary:
        return []

    known = {
        str(v).strip().lower(): str(v).strip()
        for v in vocabulary
        if isinstance(v, str) and v.strip()
    }
    if not known:
        return []

    # Compare single words and adjacent pairs: a term split in two ("hydro
    # nephrosis") only matches as a pair.
    tokens = re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", text)
    singles = {t for t in tokens if t.lower() not in known}
    # A pair is only worth checking if NEITHER half is already a correct term -
    # otherwise "hydronephrosis the" gets suggested against "hydronephrosis",
    # and a suggester that cries wolf is one nobody reads.
    pairs = {
        f"{a} {b}"
        for a, b in zip(tokens, tokens[1:])
        if a.lower() not in known and b.lower() not in known
    }
    candidates = singles | pairs

    # Phonetic index of the doctor's vocabulary, built once.
    by_sound: dict[str, str] = {}
    for lower, original in known.items():
        key = sound_key(lower.replace(" ", ""))
        if key:
            by_sound.setdefault(key, original)

    out: dict[str, Suggestion] = {}
    for candidate in candidates:
        lower = candidate.lower()
        if lower in known:
            continue  # already correct

        # 1. Spelling: catches typos and split words.
        match = difflib.get_close_matches(lower, known.keys(), n=1, cutoff=threshold)
        if match:
            score = difflib.SequenceMatcher(None, lower, match[0]).ratio()
            # A near-identical word is usually just an inflection, not an error.
            if score < 0.99:
                best = out.get(candidate)
                if best is None or score > best.confidence:
                    out[candidate] = Suggestion(candidate, known[match[0]],
                                                round(score, 2), "spelling")
                continue

        # 2. Sound: catches what edit distance cannot, where the transcriber
        #    wrote something that looks different but sounds nearly the same.
        candidate_key = sound_key(lower.replace(" ", ""))
        if len(candidate_key) < 3:
            continue
        if candidate_key in by_sound:
            suggested = by_sound[candidate_key]
            if suggested.lower() != lower and candidate not in out:
                out[candidate] = Suggestion(candidate, suggested, 0.9, "sound")
            continue
        # A close-but-not-identical sound key still shortlists.
        sound_match = difflib.get_close_matches(candidate_key, by_sound.keys(), n=1, cutoff=0.8)
        if sound_match:
            suggested = by_sound[sound_match[0]]
            if suggested.lower() != lower and candidate not in out:
                score = difflib.SequenceMatcher(None, candidate_key, sound_match[0]).ratio()
                out[candidate] = Suggestion(candidate, suggested, round(score * 0.9, 2), "sound")

    return sorted(out.values(), key=lambda s: -s.confidence)


# --------------------------------------------------------------------------- #
# Medical inverse text normalisation (ITN)
# --------------------------------------------------------------------------- #

# Compound units as spoken, mapped to ISO medical notation. Applied only when a
# number precedes them, so prose ("dose per day") is never rewritten.
_COMPOUND_UNITS = (
    (re.compile(r"(\d(?:\.\d+)?)\s*(?:milligrams?|mg)\s+per\s+(?:deciliter|decilitre|dl)\b", re.I),
     r"\1 mg/dL"),
    (re.compile(r"(\d(?:\.\d+)?)\s*(?:grams?|g)\s+per\s+(?:deciliter|decilitre|dl)\b", re.I),
     r"\1 g/dL"),
    (re.compile(r"(\d(?:\.\d+)?)\s*(?:milligrams?|mg)\s+per\s+(?:liter|litre|l)\b", re.I),
     r"\1 mg/L"),
    (re.compile(r"(\d(?:\.\d+)?)\s*(?:liters?|litres?|l)\s+per\s+min(?:ute)?\b", re.I),
     r"\1 L/min"),
    (re.compile(r"(\d(?:\.\d+)?)\s*(?:millimeters?|millimetres?|mm)\s+(?:of\s+)?mercury\b", re.I),
     r"\1 mm Hg"),
    (re.compile(r"(\d(?:\.\d+)?)\s*beats?\s+per\s+min(?:ute)?\b", re.I), r"\1 bpm"),
    (re.compile(r"(\d(?:\.\d+)?)\s*hounsfield\s+units?\b", re.I), r"\1 HU"),
    (re.compile(r"(\d(?:\.\d+)?)\s*suv\b", re.I), r"\1 SUV"),
)

_ROMAN = {"one": "I", "two": "II", "three": "III", "four": "IV", "five": "V",
          "1": "I", "2": "II", "3": "III", "4": "IV", "5": "V",
          "i": "I", "ii": "II", "iii": "III", "iv": "IV", "v": "V"}

# "grade two spondylolisthesis" -> "Grade II spondylolisthesis". Clinical
# grading is written in Roman numerals; the mapping is one-to-one, so this is
# one of the few conversions that is genuinely unambiguous.
_GRADE = re.compile(r"\bgrade[ -](one|two|three|four|five|[1-5]|iv|iii|ii|i|v)\b", re.I)

_NUMBER_TOKEN = rf"(?:{_NUMBER_WORD})(?:[ -](?:{_NUMBER_WORD}))*"
# A spoken range: "<number words> to <number words> <unit>".
_SPOKEN_RANGE = re.compile(
    rf"\b({_NUMBER_TOKEN})\s+to\s+({_NUMBER_TOKEN})\s+"
    rf"(millimetres?|millimeters?|centimetres?|centimeters?|mm|cm)\b",
    re.I,
)


def iso_units(text: str) -> tuple[str, int]:
    """Standardise compound medical units: mg/dL, mm Hg, bpm, L/min, HU, SUV."""
    changes = 0
    for pattern, replacement in _COMPOUND_UNITS:
        text, n = pattern.subn(replacement, text)
        changes += n
    return text, changes


def grades(text: str) -> tuple[str, int]:
    """Clinical grades to Roman numerals."""
    changes = 0

    def replace(match: re.Match) -> str:
        nonlocal changes
        changes += 1
        return f"Grade {_ROMAN[match.group(1).lower()]}"

    return _GRADE.sub(replace, text), changes


def protect_ambiguous_ranges(text: str) -> tuple[str, list[Suggestion], dict[str, str]]:
    """
    Find spoken ranges that would convert into nonsense, and shield them.

    "twenty two to three millimeter" converts word-by-word into "22 to 3 mm" -
    a descending range no radiologist ever dictated. The real utterance was
    "2 to 3 mm" or "22 to 23 mm", and this code cannot know which, so the
    phrase is left exactly as spoken and flagged for the radiologist. A wrong
    measurement is worse than an unconverted one - the same rule that keeps
    "two fifty" out of the number parser.

    Returns (masked_text, warnings, restore_map). Callers run the conversion
    passes on the masked text, then swap the placeholders back.
    """
    warnings: list[Suggestion] = []
    restore: dict[str, str] = {}

    def replace(match: re.Match) -> str:
        low = _words_to_number(match.group(1))
        high = _words_to_number(match.group(2))
        if low is None or high is None or float(low) < float(high):
            return match.group(0)  # converts cleanly and ascends - leave it in
        token = f"\x00RANGE{len(restore)}\x00"
        restore[token] = match.group(0)
        warnings.append(Suggestion(
            heard=match.group(0),
            suggested=f"ambiguous range - did you mean "
                      f"{str(low)[:-1] or low} to {high} or {low} to "
                      f"{str(low)[:-1]}{high}?",
            confidence=0.0,
            reason="range",
        ))
        return token

    return _SPOKEN_RANGE.sub(replace, text), warnings, restore


# --------------------------------------------------------------------------- #
# Indic-English anchor vocabulary
# --------------------------------------------------------------------------- #

# Radiology terms that Indian-accent ASR reliably splits into two words. When
# adjacent words concatenate (letter-for-letter) into one of these, joining
# them changes no letters - so it is applied, and counted, not just suggested.
ANCHOR_TERMS = (
    "subcentimeter", "subcentimetric", "pneumothorax", "pneumonia",
    "pneumoperitoneum", "hydronephrosis", "hydroureter", "cholelithiasis",
    "cholecystitis", "choledocholithiasis", "hepatosplenomegaly",
    "hepatomegaly", "splenomegaly", "cardiomegaly", "lymphadenopathy",
    "spondylolisthesis", "spondylosis", "echotexture", "bronchiectasis",
    "atelectasis", "consolidation", "retroperitoneal", "paraaortic",
    "periportal", "pericholecystic", "perinephric", "endometrium",
    "myometrium", "nephrolithiasis", "urolithiasis", "osteophytes",
    "pleuropulmonary", "cardiothoracic", "costophrenic", "tracheobronchial",
)

# Mishearings where the letters ARE different - these are never applied,
# only suggested, exactly like near_misses(). heard -> meant.
ANCHOR_MISHEARINGS = {
    "o capacity": "opacity",
    "oh capacity": "opacity",
    "new monia": "pneumonia",
    "new mo thorax": "pneumothorax",
    "numo thorax": "pneumothorax",
    "colic list": "cholelithiasis",
    "eco texture": "echotexture",
    "a telecast is": "atelectasis",
    "sub center meter": "subcentimeter",
    "grade to": "grade two",
}

_ANCHOR_BY_JOIN = {t.lower(): t for t in ANCHOR_TERMS}


def join_split_terms(text: str) -> tuple[str, int]:
    """
    Rejoin anchor terms the transcriber split: "hydro nephrosis" ->
    "hydronephrosis". Safe to apply because the letters are identical -
    nothing is rewritten, only a space removed.
    """
    changes = 0
    tokens = re.split(r"(\s+)", text)
    words = [(i, t) for i, t in enumerate(tokens) if t and not t.isspace()]
    i = 0
    while i < len(words) - 1:
        merged = False
        for span in (3, 2):  # try three-word joins first
            if i + span - 1 >= len(words):
                continue
            parts = [words[i + k][1] for k in range(span)]
            joined = "".join(parts).lower()
            if joined in _ANCHOR_BY_JOIN:
                first_index = words[i][0]
                last_index = words[i + span - 1][0]
                replacement = _ANCHOR_BY_JOIN[joined]
                if parts[0][:1].isupper():
                    replacement = replacement.capitalize()
                tokens[first_index] = replacement
                for j in range(first_index + 1, last_index + 1):
                    tokens[j] = ""
                changes += 1
                words = [(k, t) for k, t in enumerate(tokens) if t and not t.isspace()]
                merged = True
                break
        if not merged:
            i += 1
    return "".join(tokens), changes


def anchor_suggestions(text: str) -> list[Suggestion]:
    """Known Indic-accent mishearings present in the text. Suggested, never applied."""
    out: list[Suggestion] = []
    lowered = text.lower()
    for heard, meant in ANCHOR_MISHEARINGS.items():
        if re.search(r"\b" + re.escape(heard) + r"\b", lowered):
            out.append(Suggestion(heard=heard, suggested=meant,
                                  confidence=0.95, reason="anchor"))
    return out


# --------------------------------------------------------------------------- #


@dataclass
class Cleanup:
    text: str
    number_fixes: int = 0
    unit_fixes: int = 0
    itn_fixes: int = 0
    suggestions: list[Suggestion] = None

    @property
    def changed(self) -> bool:
        return bool(self.number_fixes or self.unit_fixes or self.itn_fixes)

    @property
    def note(self) -> str:
        bits = []
        if self.number_fixes:
            bits.append(f"{self.number_fixes} spoken number(s) written as figures")
        if self.unit_fixes:
            bits.append(f"{self.unit_fixes} unit(s) normalised")
        if self.itn_fixes:
            bits.append(f"{self.itn_fixes} medical notation fix(es)")
        return " · ".join(bits)


def clean(text: str, vocabulary: list[str] | None = None) -> Cleanup:
    """Everything above, in the right order."""
    # Shield ranges that would convert into nonsense BEFORE any conversion runs.
    out, range_warnings, restore = protect_ambiguous_ranges(text)

    out, numbers = spoken_numbers(out)
    out, unit_count = units(out)
    out, compound_count = iso_units(out)
    out, grade_count = grades(out)
    out, join_count = join_split_terms(out)

    for token, original in restore.items():
        out = out.replace(token, original)

    suggestions = near_misses(out, vocabulary or [])
    suggestions.extend(anchor_suggestions(out))
    suggestions.extend(range_warnings)

    return Cleanup(
        text=out,
        number_fixes=numbers,
        unit_fixes=unit_count,
        itn_fixes=compound_count + grade_count + join_count,
        suggestions=suggestions,
    )
