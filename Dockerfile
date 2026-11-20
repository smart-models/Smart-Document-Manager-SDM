# syntax=docker/dockerfile:1.7

# OCR API dependency environment.
FROM python:3.11-slim-bookworm@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b AS ocr-api-builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python3 -m venv --copies /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /app
COPY requirements/api.txt requirements/api.txt
# Everything from PyPI except paddlepaddle-gpu, which only the PaddlePaddle CUDA
# index publishes: that single exact pin is fetched there without dependencies.
# The index publishes no hashes, so the cp311 manylinux wheel is bound to the
# sha256 recorded when it was reviewed; a different file fails the build.
RUN pip install --upgrade pip==26.2.1 setuptools==84.2.1 \
    && grep -v '^paddlepaddle-gpu==' requirements/api.txt > /tmp/pypi.txt \
    && pip install --no-compile -r /tmp/pypi.txt \
    && echo "$(grep '^paddlepaddle-gpu==' requirements/api.txt) --hash=sha256:e14872e9cfe50f38961a420592cbded6160c332eb5f3dfdfd2731873582c3e65" > /tmp/paddle-gpu.txt \
    && pip install --no-compile --no-deps --require-hashes \
        --index-url https://www.paddlepaddle.org.cn/packages/stable/cu126/ \
        -r /tmp/paddle-gpu.txt \
    && pip check \
    && rm /tmp/pypi.txt /tmp/paddle-gpu.txt

# OCR API runtime.
FROM python:3.11-slim-bookworm@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b AS ocr-api

# Must equal APP_VERSION in ocrserver/schemas.py (tests/test_release_version.py).
ARG SMART_DOCUMENT_MANAGER_VERSION=1.0.1
LABEL org.opencontainers.image.title="ocr-api" \
      org.opencontainers.image.description="Smart Document Manager FastAPI server (PaddleOCR-VL-1.6 via vLLM)" \
      org.opencontainers.image.version="${SMART_DOCUMENT_MANAGER_VERSION}" \
      org.opencontainers.image.licenses="AGPL-3.0-only"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_COMPILE=1 \
    PATH="/opt/venv/bin:$PATH"

# System libs required at runtime: libgomp1 (paddlepaddle OpenMP core),
# libgl1 + libglib2.0-0 (OpenCV used by the PaddleOCR pipeline).
# Headless LibreOffice (no Java, no GUI) converts Office/ODF input to PDF;
# Liberation/DejaVu are metric-compatible fonts so layouts keep their geometry.
# Adds roughly 400-500 MB to the image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgomp1 \
        libgl1 \
        libglib2.0-0 \
        libreoffice-core-nogui \
        libreoffice-writer-nogui \
        libreoffice-impress-nogui \
        libreoffice-calc-nogui \
        fonts-liberation \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

WORKDIR /app
COPY --from=ocr-api-builder --chown=appuser:appuser /opt/venv /opt/venv
# One COPY per directory: a multi-source `COPY a/ b/ dest/` copies the *contents*
# of every source into dest, flattening the packages and destroying importability.
COPY --chown=appuser:appuser ocrserver/ ./ocrserver/
COPY --chown=appuser:appuser storage/ ./storage/
COPY --chown=appuser:appuser vllm/ ./vllm/
COPY --chown=appuser:appuser calibration/ ./calibration/
COPY --chown=appuser:appuser frontend/dist/ ./frontend/dist/
# LICENSE and NOTICE travel with the code; NOTICE points at the third-party
# licence texts under frontend/dist/ and calibration/fonts/.
COPY --chown=appuser:appuser \
    ocr_server.py \
    autotune_entrypoint.py \
    benchmark.py \
    LICENSE \
    NOTICE \
    ./

RUN mkdir -p /app/model_cache/autotune && chown -R appuser:appuser /app/model_cache
# Pre-create the paddlex cache dir owned by appuser so a fresh named volume
# mounted here inherits uid 1000 ownership (root fs is read-only at runtime).
RUN mkdir -p /home/appuser/.paddlex && chown -R appuser:appuser /home/appuser/.paddlex

USER appuser
EXPOSE 8020
CMD ["python3", "autotune_entrypoint.py"]

# MCP server runtime.
FROM python:3.11-slim-bookworm@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b AS ocr-mcp

# Must equal ocr_mcp.__version__ (tests/test_release_version.py).
ARG SMART_DOCUMENT_MANAGER_VERSION=1.0.0
LABEL org.opencontainers.image.title="personal-smart-ocr-mcp" \
      org.opencontainers.image.description="MCP server exposing Smart Document Manager" \
      org.opencontainers.image.version="${SMART_DOCUMENT_MANAGER_VERSION}" \
      org.opencontainers.image.licenses="AGPL-3.0-only"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
# LICENSE is the wheel's license-files entry; the build fails without it.
COPY ocr-mcp-server/pyproject.toml ocr-mcp-server/README.md ocr-mcp-server/LICENSE ./
COPY ocr-mcp-server/ocr_mcp ./ocr_mcp
RUN pip install --no-cache-dir --upgrade pip==26.2.1 setuptools==84.2.1 \
    && pip install --no-cache-dir . \
    && groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid appuser --create-home appuser

USER appuser
EXPOSE 8021
CMD ["python", "-m", "ocr_mcp"]
