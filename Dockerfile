# syntax=docker/dockerfile:1.7
# Africam Bird image. Single image runs both the pipeline supervisor and
# the web app — docker-compose picks the entrypoint per service.
#
# Tested on linux/arm64 (Raspberry Pi 5) and linux/amd64. TensorFlow is
# heavy on disk (~600 MB) but birdnetlib only uses tf.lite.Interpreter, so
# at runtime memory stays modest (~200 MB resident per service).

FROM python:3.12-slim-bookworm

# ffmpeg: audio decode + spectrogram render
# nodejs: yt-dlp's n-sig solver for YouTube anti-bot challenges
# curl + ca-certificates: outbound HTTPS (Wikipedia, Xeno-Canto)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        nodejs \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv is already arm64-ready and gives us reproducible installs from the
# committed lockfile.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Install deps first (cached layer when only source changes).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Copy the project and install it editable into the same venv.
COPY src ./src
COPY scripts ./scripts
RUN uv sync --frozen --no-dev

# Compose overrides this per service. Default to the pipeline so a bare
# `docker run africam` does the right thing.
CMD ["uv", "run", "africam", "run"]
