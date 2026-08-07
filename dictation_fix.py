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

    # Compare single words and adjacent pairs: "colic list" is two words for one term.
    tokens = re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", text)
    candidates = set(tokens) | {
        f"{a} {b}" for a, b in zip(tokens, tokens[1:])
    }

    out: dict[str, Suggestion] = {}
    for candidate in candidates:
        lower = candidate.lower()
        if lower in known:
            continue  # already correct
        match = difflib.get_close_matches(lower, known.keys(), n=1, cutoff=threshold)
        if not match:
            continue
        score = difflib.SequenceMatcher(None, lower, match[0]).ratio()
        # A near-identical word is usually just an inflection, not an error.
        if score >= 0.99:
            continue
        best = out.get(candidate)
        if best is None or score > best.confidence:
            out[candidate] = Suggestion(candidate, known[match[0]], round(score, 2))

    return sorted(out.values(), key=lambda s: -s.confidence)


# --------------------------------------------------------------------------- #


@dataclass
class Cleanup:
    text: str
    number_fixes: int = 0
    unit_fixes: int = 0
    suggestions: list[Suggestion] = None

    @property
    def changed(self) -> bool:
        return bool(self.number_fixes or self.unit_fixes)

    @property
    def note(self) -> str:
        bits = []
        if self.number_fixes:
            bits.append(f"{self.number_fixes} spoken number(s) written as figures")
        if self.unit_fixes:
            bits.append(f"{self.unit_fixes} unit(s) normalised")
        return " · ".join(bits)


def clean(text: str, vocabulary: list[str] | None = None) -> Cleanup:
    """Everything above, in the right order."""
    out, numbers = spoken_numbers(text)
    out, unit_count = units(out)
    return Cleanup(
        text=out,
        number_fixes=numbers,
        unit_fixes=unit_count,
        suggestions=near_misses(out, vocabulary or []),
    )
