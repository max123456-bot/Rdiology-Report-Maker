"""
Word-loss audit.

Reads the generated .docx back off disk (not the in-memory Block list, so a
rendering bug cannot hide from the check) and compares its tokens against the
raw input the user pasted.  Case is ignored because HC FORMAT mandates
UPPERCASE titles and headings; bullet glyphs are ignored because Word supplies
its own.  Everything else - every word, every measurement, every number - must
match exactly, in count.
"""

from __future__ import annotations

import io
import re
from collections import Counter
from dataclasses import dataclass, field

from docx import Document

from hc_format import _BULLET_PREFIX

# Words / numbers / measurements. Keeps decimals and hyphenated terms together.
_TOKEN = re.compile(r"[A-Za-z]+(?:['\-][A-Za-z]+)*|\d+(?:[.,]\d+)*")
_NUMERIC = re.compile(r"^\d")
# Glyphs a list marker may have used in the source; Word renders bullets itself.
_IGNORED = {"•", "●", "▪", "‣", "⁃", "·", "*", "-", "–", "—"}


@dataclass
class AuditResult:
    ok: bool = False
    source_tokens: int = 0
    output_tokens: int = 0
    missing: list[tuple[str, int]] = field(default_factory=list)  # in source, short in output
    added: list[tuple[str, int]] = field(default_factory=list)  # in output, absent from source
    numbers_ok: bool = True
    missing_numbers: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.ok:
            return (
                f"PASS - all {self.source_tokens} words and numbers preserved verbatim "
                f"(case-insensitive; bullet glyphs excluded)."
            )
        bits = []
        if self.missing:
            bits.append(f"{sum(c for _, c in self.missing)} token(s) missing from output")
        if self.added:
            bits.append(f"{sum(c for _, c in self.added)} token(s) added")
        if not self.numbers_ok:
            bits.append(f"{len(self.missing_numbers)} measurement/number mismatch")
        return "FAIL - " + "; ".join(bits)


def _tokenize(text: str) -> Counter:
    return Counter(t.lower() for t in _TOKEN.findall(text) if t not in _IGNORED)


def _strip_list_markers(text: str) -> str:
    """
    Drop the same leading list markers the parser drops ("1.", "a)", "-", "•").

    Word draws its own bullet, so a source "1." is presentation, not content.
    The regex demands whitespace after the marker, so a measurement that opens a
    line ("2.4 x 1.9 cm lesion...") is left untouched.
    """
    return "\n".join(_BULLET_PREFIX.sub("", line) for line in text.split("\n"))


def docx_text(docx_bytes: bytes) -> str:
    """Flatten a .docx back to plain text, including headers and tables."""
    doc = Document(io.BytesIO(docx_bytes))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells)
    for section in doc.sections:
        parts.extend(p.text for p in section.header.paragraphs)
        parts.extend(p.text for p in section.footer.paragraphs)
    return "\n".join(parts)


def audit(
    raw_text: str,
    docx_bytes: bytes,
    *,
    letterhead_text: str = "",
    page_numbers: bool = False,
    preserve_as_is: bool = False,
) -> AuditResult:
    """
    Compare raw input against the rendered document. No word may go missing.

    In as-is mode nothing is stripped on either side, because the engine did not
    strip anything either - a "1." that opened a line is still a "1." in the
    output, so it must still be counted.
    """
    src = _tokenize(raw_text if preserve_as_is else _strip_list_markers(raw_text))
    out = _tokenize(docx_text(docx_bytes))

    # Text this app injected on purpose (letterhead, sign-off, "Page x of y").
    injected = letterhead_text + (" Page of" if page_numbers else "")
    for token, count in _tokenize(injected).items():
        out[token] = max(0, out[token] - count)

    missing = [(tok, src[tok] - out.get(tok, 0)) for tok in src if src[tok] > out.get(tok, 0)]
    added = [(tok, out[tok] - src.get(tok, 0)) for tok in out if out[tok] > src.get(tok, 0)]

    missing_numbers = sorted(
        {tok for tok, _ in missing if _NUMERIC.match(tok)}
    )

    result = AuditResult(
        source_tokens=sum(src.values()),
        output_tokens=sum(out.values()),
        missing=sorted(missing, key=lambda x: -x[1]),
        added=sorted(added, key=lambda x: -x[1]),
        numbers_ok=not missing_numbers,
        missing_numbers=missing_numbers,
    )
    result.ok = not result.missing and not result.added
    return result
