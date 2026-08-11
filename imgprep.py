"""
Image preparation and hybrid OCR for scanned reports.

Faded thermal prints, phone photos taken at an angle, photocopies of
photocopies - the documents a clinic actually has. Raw, they make any OCR
engine hallucinate. This module cleans them up first, then reads them with
the cheapest engine that is confident enough:

    preprocess()   OpenCV pipeline: grayscale -> bilateral denoise (strips
                   paper grain without blurring letter edges) -> CLAHE
                   (recovers faded low-contrast text) -> deskew (rotates the
                   page to 0 degrees) -> adaptive threshold (evens out
                   uneven lighting).

    hybrid_ocr()   Step 1: local Tesseract on the cleaned image - free,
                   offline, milliseconds.
                   Step 2: if its own confidence is below the bar (85%), the
                   cleaned image goes to Gemini Vision instead.
                   The result says which engine read it and how sure it was;
                   the user always sees the text before it becomes a report.

Every dependency is optional and every absence is honest: no OpenCV means
the raw image goes straight through; no Tesseract means Gemini is tried
directly; no key means a clear error, not a silent empty string.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

CONFIDENCE_BAR = 85.0   # below this, local OCR is not trusted
_MIN_DESKEW_DEG = 0.3   # rotations smaller than this are noise, not skew


@dataclass
class ProcessedImage:
    png: bytes
    deskew_degrees: float = 0.0
    steps: list[str] = field(default_factory=list)


@dataclass
class OcrResult:
    text: str
    engine: str          # tesseract | gemini | none
    confidence: float    # 0-100; Gemini reports -1 (it exposes none)
    note: str = ""


# --------------------------------------------------------------------------- #
# OpenCV pipeline
# --------------------------------------------------------------------------- #


def preprocess(image_bytes: bytes) -> ProcessedImage:
    """
    Clean one scanned page. Raises ValueError when the bytes are not an
    image; returns the input untouched (with a note) when OpenCV is absent.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return ProcessedImage(png=image_bytes,
                              steps=["opencv not installed - image passed through"])

    array = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Not a decodable image.")

    steps: list[str] = []
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    steps.append("grayscale")

    # Bilateral: smooths paper grain while keeping letter edges crisp, which
    # is exactly the trade a Gaussian blur gets wrong.
    gray = cv2.bilateralFilter(gray, d=9, sigmaColor=75, sigmaSpace=75)
    steps.append("bilateral denoise")

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)
    steps.append("CLAHE contrast")

    gray, angle = _deskew(gray, cv2, np)
    if abs(angle) >= _MIN_DESKEW_DEG:
        steps.append(f"deskewed {angle:+.1f} deg")

    # Gaussian adaptive threshold rides out the uneven lighting of a phone
    # photo; a global threshold would black out half the page.
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY,
        blockSize=31, C=15,
    )
    steps.append("adaptive threshold")

    ok, encoded = cv2.imencode(".png", binary)
    if not ok:
        raise ValueError("Could not encode the processed image.")
    return ProcessedImage(png=encoded.tobytes(), deskew_degrees=angle, steps=steps)


def _deskew(gray, cv2, np):
    """Estimate the text angle from the ink mask and rotate the page level."""
    inverted = cv2.threshold(gray, 0, 255,
                             cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    coordinates = np.column_stack(np.where(inverted > 0))
    if len(coordinates) < 50:
        return gray, 0.0

    angle = cv2.minAreaRect(coordinates)[-1]
    # minAreaRect's angle convention changed across OpenCV versions; fold
    # both into the same [-45, 45] window.
    if angle > 45:
        angle -= 90
    elif angle < -45:
        angle += 90
    if abs(angle) < _MIN_DESKEW_DEG or abs(angle) > 30:
        # Near-zero is noise; huge angles are almost always the rect flipping
        # on dense text, and rotating 40 degrees on a guess destroys the page.
        return gray, 0.0

    # `angle` is the measured skew of the text; the CORRECTION is its
    # negation (getRotationMatrix2D's positive direction is counter-clockwise).
    # Rotating by +angle doubles the skew - proven empirically in
    # master_check by re-measuring the corrected page.
    correction = -angle
    height, width = gray.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), correction, 1.0)
    rotated = cv2.warpAffine(
        gray, matrix, (width, height),
        flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=255,
    )
    return rotated, float(correction)


# --------------------------------------------------------------------------- #
# OCR engines
# --------------------------------------------------------------------------- #


def local_ocr(png_bytes: bytes) -> OcrResult | None:
    """
    Tesseract on a cleaned image. None when Tesseract (the Python package or
    the binary) is not installed - the caller falls through to Gemini.
    """
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return None

    try:
        image = Image.open(io.BytesIO(png_bytes))
        data = pytesseract.image_to_data(image,
                                         output_type=pytesseract.Output.DICT)
    except Exception:
        return None  # binary missing or broken install - not this module's fight

    words: list[str] = []
    confidences: list[float] = []
    lines: dict[tuple, list[str]] = {}
    for i, word in enumerate(data.get("text", [])):
        conf = float(data["conf"][i]) if str(data["conf"][i]).lstrip("-").isdigit() else -1.0
        if not str(word).strip() or conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(str(word))
        words.append(str(word))
        confidences.append(conf)

    if not words:
        return OcrResult(text="", engine="tesseract", confidence=0.0,
                         note="Tesseract found no text.")
    text = "\n".join(" ".join(ws) for _, ws in sorted(lines.items()))
    return OcrResult(
        text=text,
        engine="tesseract",
        confidence=round(sum(confidences) / len(confidences), 1),
    )


def hybrid_ocr(image_bytes: bytes, mime_type: str,
               api_key: str = "", model: str = "") -> OcrResult:
    """
    The fallback hierarchy: clean -> Tesseract -> (below the bar) -> Gemini.

    Raises ValueError only when nothing at all can read the image - no local
    engine and no key.
    """
    try:
        processed = preprocess(image_bytes)
        cleaned, cleaned_mime = processed.png, "image/png"
        prep_note = ", ".join(processed.steps)
    except ValueError:
        # PDFs land here (not a raster image) - send the original to Gemini.
        processed = None
        cleaned, cleaned_mime = image_bytes, mime_type
        prep_note = "no raster preprocessing"

    local = local_ocr(cleaned) if processed is not None else None
    if local and local.confidence >= CONFIDENCE_BAR:
        local.note = f"Read locally (free, offline) at {local.confidence}% · {prep_note}"
        return local

    if api_key and model:
        import ai_parser

        text = ai_parser.extract_text_from_file(cleaned, cleaned_mime, api_key, model)
        note = f"Gemini Vision · {prep_note}"
        if local and local.text:
            agreement = _token_agreement(local.text, text)
            note += f" · local OCR was {local.confidence}% confident, {agreement}% token agreement"
        return OcrResult(text=text, engine="gemini", confidence=-1.0, note=note)

    if local is not None:
        local.note = (f"Local OCR only ({local.confidence}% confident - below the "
                      f"{CONFIDENCE_BAR:.0f}% bar). Switch on the AI engine for a "
                      "better read. · " + prep_note)
        return local

    raise ValueError(
        "Nothing can read this image: Tesseract is not installed and no Gemini "
        "key is configured. Install tesseract-ocr + pytesseract, or switch the "
        "engine to AI-assisted."
    )


def _token_agreement(a: str, b: str) -> int:
    """Rough % of one text's tokens found in the other - a disagreement alarm."""
    tokens_a = {t.lower() for t in a.split() if len(t) > 2}
    tokens_b = {t.lower() for t in b.split() if len(t) > 2}
    if not tokens_a or not tokens_b:
        return 0
    overlap = len(tokens_a & tokens_b)
    return round(100 * overlap / min(len(tokens_a), len(tokens_b)))
