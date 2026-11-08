# ScoreForge - container image
#
# The image bundles ffmpeg and MuseScore 4 (both needed for audio decoding
# and engraving). Model weights are downloaded at build time so the first
# request does not pay for a 428 MB download.

FROM python:3.11-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    HF_HOME=/opt/models \
    SCOREFORGE_DATA_DIR=/data

# --- system dependencies -------------------------------------------------- #
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        git \
        ca-certificates \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
        libfontconfig1 \
        libx11-6 \
        libxcb1 \
        libxcb-cursor0 \
        libxkbcommon0 \
        libasound2 \
        libdbus-1-3 \
        xvfb \
        xauth \
        fonts-dejavu \
        fonts-liberation \
        fonts-freefont-ttf \
    && rm -rf /var/lib/apt/lists/*

# --- uv ------------------------------------------------------------------- #
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# --- MuseScore 4 ---------------------------------------------------------- #
# The release tag and the build number inside the file name differ.
ARG MUSESCORE_TAG=v4.7.6
ARG MUSESCORE_BUILD=4.7.6.261022093
RUN mkdir -p /opt/musescore \
    && curl -fsSL -o /tmp/musescore.AppImage \
        "https://github.com/musescore/MuseScore/releases/download/${MUSESCORE_TAG}/MuseScore-Studio-${MUSESCORE_BUILD}-x86_64.AppImage" \
    && chmod +x /tmp/musescore.AppImage \
    && cd /opt/musescore \
    && /tmp/musescore.AppImage --appimage-extract > /dev/null \
    && rm -f /tmp/musescore.AppImage \
    && chmod +x /opt/musescore/squashfs-root/AppRun \
        /opt/musescore/squashfs-root/bin/mscore4portable \
    && printf '%s\n' \
        '#!/bin/sh' \
        '# MuseScore 4 needs a display; a virtual one is enough.' \
        'export APPDIR=/opt/musescore/squashfs-root' \
        'export QT_PLUGIN_PATH=$APPDIR/plugins' \
        'export LD_LIBRARY_PATH=$APPDIR/lib:$LD_LIBRARY_PATH' \
        'exec xvfb-run -a --server-args="-screen 0 1600x1200x24" $APPDIR/AppRun "$@"' \
        > /usr/local/bin/mscore \
    && chmod +x /usr/local/bin/mscore \
    && ln -sf /usr/local/bin/mscore /usr/local/bin/musescore \
    && ln -sf /usr/local/bin/mscore /usr/local/bin/musescore-portable \
    && ln -sf /usr/local/bin/mscore /usr/local/bin/musescore4 \
    && ln -sf /usr/local/bin/mscore /usr/local/bin/mscore4portable

ENV SCOREFORGE_MUSESCORE=/usr/local/bin/mscore
ENV MUSESCORE_HOME=/opt/musescore

# --- python dependencies -------------------------------------------------- #
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --extra web --no-dev \
    && uv pip install --python /app/.venv/bin/python huggingface_hub \
    && .venv/bin/python -c "\
from huggingface_hub import snapshot_download;\
snapshot_download('Wayne42/DuET', allow_patterns=['config.json','model.safetensors']);\
print('model cached')"

# --- application ---------------------------------------------------------- #
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH=/app/src

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "scoreforge.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
