# Render (or any Docker host) image for the HC FORMAT app.
#
# Docker rather than Render's native Python runtime for one reason: system
# packages. The speech path needs ffmpeg, the free OCR tier needs tesseract,
# and a native runtime gives us neither. This image is the same environment
# everywhere - local, Render, or a clinic server.
#
# Python 3.11 to match the version the test suites run on.

FROM python:3.11-slim

# ffmpeg        - audio conversion for the dictation engine (pydub)
# tesseract-ocr - the free local OCR tier (imgprep.py); Gemini is the fallback
# libglib2.0-0  - runtime dependency of opencv-python-headless
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        tesseract-ocr \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements first, so code edits do not invalidate the pip layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

# Render injects PORT. The default keeps `docker run` working locally.
# Secrets (GEMINI_API_KEY, STORAGE_URL, ACCESS_CODE...) come from environment
# variables - every secret reader in this codebase falls back to the
# environment when .streamlit/secrets.toml is absent.
EXPOSE 8501
CMD streamlit run app.py \
    --server.port ${PORT:-8501} \
    --server.address 0.0.0.0 \
    --server.headless true \
    --server.enableCORS false \
    --server.enableXsrfProtection true \
    --browser.gatherUsageStats false
