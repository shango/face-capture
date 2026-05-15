# Multi-stage build for the face-capture web service.
#
# Stage 1 (web): builds the Vite SPA into /build/web/dist.
# Stage 2 (runtime): python:3.11-slim with ffmpeg and the MediaPipe model,
#   copies the SPA build, and launches uvicorn.

# ---- Stage 1: SPA build ----------------------------------------------------
FROM node:20-slim AS web

WORKDIR /build/web

# Install JS deps using only the lockfile + manifest for cache friendliness.
COPY web/package.json web/package-lock.json* ./
RUN npm ci

COPY web/ ./
RUN npm run build


# ---- Stage 2: Python runtime ----------------------------------------------
FROM python:3.11-slim AS runtime

# System deps:
#   - ffmpeg          video decoding / normalization for the pipeline
#   - libgl1, libglib2.0-0, libsm6, libxext6, libxrender1
#                     OpenCV + MediaPipe shared-library requirements
#   - ca-certificates needed for the MediaPipe model fetch and R2 calls
#   - curl            used at build time to download the model
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
        ca-certificates \
        curl \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install Python deps first so application-code changes don't bust the layer.
COPY requirements.txt ./
RUN pip install --upgrade pip \
 && pip install -r requirements.txt

# Application code.
COPY app/ ./app/
COPY pipeline/ ./pipeline/

# MediaPipe face-landmarker model — pinned to the float16 v1 build used by
# the local pipeline. Fetched at build time so the runtime image is
# self-contained.
RUN curl -fSL \
      https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task \
      -o pipeline/face_landmarker.task

# Built SPA from stage 1.
COPY --from=web /build/web/dist ./web/dist

# Entrypoint execs uvicorn so signals reach the server.
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# Run as non-root.
RUN useradd --create-home --shell /bin/bash app \
 && chown -R app:app /app
USER app

# Railway injects $PORT at runtime; default to 8000 for local docker run.
ENV PORT=8000
EXPOSE 8000

CMD ["./entrypoint.sh"]
