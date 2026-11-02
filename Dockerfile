# =============================================================================
# Build stages: `toolchain` (compiler + Python, shared), `model` (the credit
# text detection model) and `builder` (dependency and app wheels)
# =============================================================================
FROM ghcr.io/linuxserver/baseimage-ubuntu:noble@sha256:cc0fb3598832cdfeb3bcdb8b170a232bba51ab4d769f59b079cc7138bc7f763b AS base

FROM base AS toolchain

# Build-time only: compiler toolchain + Python + git (for setuptools-scm)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    gcc musl-dev python3 python3-pip git && \
    rm -rf /var/lib/apt/lists/*

ENV PIP_BREAK_SYSTEM_PACKAGES=1

# Build the CPU-focused fallback and retain its exact source and build recipe.
FROM toolchain AS ffmpeg-gpl-builder
RUN arch="$(dpkg --print-architecture)" && \
    if [ "$arch" = amd64 ]; then mirror=http://archive.ubuntu.com/ubuntu; \
    else mirror=http://ports.ubuntu.com/ubuntu-ports; fi && \
    printf "deb-src %s noble main universe\ndeb-src %s noble-updates main universe\ndeb-src %s noble-security main universe\n" "$mirror" "$mirror" "$mirror" > /etc/apt/sources.list.d/ffmpeg-source.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
      make g++ dpkg-dev nasm pkg-config xz-utils curl ca-certificates \
      zlib1g-dev libbz2-dev liblzma-dev libgnutls28-dev libzimg-dev libchromaprint-dev && \
    rm -rf /var/lib/apt/lists/*
COPY docker/ffmpeg/build.sh /tmp/ffmpeg-gpl-build.sh
COPY Dockerfile /tmp/ffmpeg-gpl-Dockerfile
ARG FFMPEG_JOBS=4
RUN apt-get update && FFMPEG_JOBS="$FFMPEG_JOBS" bash /tmp/ffmpeg-gpl-build.sh && \
    rm -rf /var/lib/apt/lists/*

# The optimized loudness analyzer remains a separate CPU-only binary.
FROM toolchain AS ffmpeg-loudness-builder
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      make g++ nasm pkg-config xz-utils patch curl ca-certificates \
      zlib1g-dev libbz2-dev liblzma-dev && \
    rm -rf /var/lib/apt/lists/*
COPY docker/ffmpeg-loudness/build.sh docker/ffmpeg-loudness/ebur128.patch docker/ffmpeg-loudness/README.md /tmp/ffmpeg-loudness/
ARG FFMPEG_LOUDNESS_JOBS=4
RUN chmod +x /tmp/ffmpeg-loudness/build.sh && \
    FFMPEG_LOUDNESS_JOBS="$FFMPEG_LOUDNESS_JOBS" /tmp/ffmpeg-loudness/build.sh

# Credit text models (Intro & Credits, spec §5.4): PP-OCRv4 detection from the pinned rapidocr_onnxruntime 1.4.4 wheel
# and PP-OCRv5 Latin recognition from RapidOCR's model repository at a pinned tag, the wheel and both models verified
# by sha256 (scripts/fetch_textdet_model.py). A stage of its own, so an edit to the fetch script never invalidates the
# dependency wheel cache (Layer A), and BuildKit fetches it while the wheels build.
FROM toolchain AS model
COPY scripts/fetch_textdet_model.py /tmp/fetch_textdet_model.py
RUN python3 /tmp/fetch_textdet_model.py --out /models

FROM toolchain AS builder
WORKDIR /build

# ---------------------------------------------------------------------------
# Layer A: pre-cache DEPENDENCY wheels via a stub package.
#
# This layer is keyed only on pyproject.toml. Without the split, every
# Python source change at the next layer would invalidate the wheel cache
# and force pip to rebuild ~50 dep wheels (Flask, plexapi, APScheduler,
# gunicorn, transitive deps) from scratch — adding 30-60s per build job
# (×4 parallel jobs in CI = ~30-60s wall-clock per push).
#
# The stub uses setuptools-scm's fallback_version so no git history is
# needed for this layer; the real version gets stamped at Layer B below.
# ---------------------------------------------------------------------------
COPY pyproject.toml ./
RUN mkdir -p media_preview_generator && \
    touch media_preview_generator/__init__.py && \
    SETUPTOOLS_SCM_PRETEND_VERSION_FOR_REPRISE=0.0.0 \
      pip3 wheel --wheel-dir=/wheels --no-cache-dir . && \
    # Drop the stub app wheel — Layer B builds the real one.
    rm -f /wheels/reprise-*.whl && \
    rm -rf media_preview_generator

# ---------------------------------------------------------------------------
# Layer B: build the APP wheel only, reusing the cached deps above.
#
# --no-deps tells pip to skip dep resolution (already pre-built).
# Source-only changes invalidate just this layer (~5-10s of work),
# keeping iteration speed high.
#
# The version ARG is declared just before its only use: every RUN after an ARG
# sees it as an environment variable, and CI passes a new version on every
# build, so declared any earlier it would invalidate Layer A's cache each time.
# ---------------------------------------------------------------------------
COPY media_preview_generator/ ./media_preview_generator/
ARG SETUPTOOLS_SCM_PRETEND_VERSION=""
RUN if [ -n "$SETUPTOOLS_SCM_PRETEND_VERSION" ]; then \
      SETUPTOOLS_SCM_PRETEND_VERSION_FOR_REPRISE=$SETUPTOOLS_SCM_PRETEND_VERSION \
      pip3 wheel --wheel-dir=/wheels --no-cache-dir --no-deps .; \
    else \
      pip3 wheel --wheel-dir=/wheels --no-cache-dir --no-deps .; \
    fi

# =============================================================================
# Runtime stage — lean production image (no compiler toolchain)
# =============================================================================
FROM base

# Runtime dependencies, including the existing Intel graphics repository.
#
# Existing graphics drivers are retained; the bundled FFmpeg build is CPU-focused.
# - libvpl2 / libmfx-gen1: Intel oneVPL runtime (QSV).
# - libva2, libva-drm2: VA-API libraries
# - vainfo: Tool to test/verify VA-API functionality
# - pciutils: Provides lspci for better GPU naming
# - git: For version detection (version_check.get_git_commit_sha) when /app is a git checkout
# - mediainfo: For media file metadata
#
# FFmpeg 8.1.2 includes the fix for CVE-2026-8461 (PixelSmash).
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      mediainfo python3 python3-pip gosu pciutils git curl gnupg ca-certificates \
      mesa-va-drivers mesa-vulkan-drivers libva2 libva-drm2 vainfo \
      zlib1g libbz2-1.0 liblzma5 libgnutls30t64 libzimg2 libchromaprint1 && \
    if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
      # Intel graphics apt repo (Ubuntu Noble) for newer VAAPI + OpenCL
      curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors --retry-connrefused \
        https://repositories.intel.com/gpu/intel-graphics.key \
        | gpg --dearmor -o /usr/share/keyrings/intel-graphics.gpg && \
      echo "deb [arch=amd64 signed-by=/usr/share/keyrings/intel-graphics.gpg] https://repositories.intel.com/gpu/ubuntu noble unified" \
        > /etc/apt/sources.list.d/intel-graphics.list && \
      apt-get update && \
      apt-get install -y --no-install-recommends \
        intel-media-va-driver-non-free \
        i965-va-driver \
        libmfx-gen1 \
        libvpl2 \
        intel-opencl-icd \
        ocl-icd-libopencl1; \
    fi && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install the bundled FFmpeg and ffprobe.
COPY --from=ffmpeg-gpl-builder /opt/ffmpeg-gpl /opt/ffmpeg-gpl
RUN ln -s /opt/ffmpeg-gpl/bin/ffmpeg /usr/local/bin/ffmpeg && \
    ln -s /opt/ffmpeg-gpl/bin/ffprobe /usr/local/bin/ffprobe

# Install pre-built wheels from builder (no compiler needed)
COPY --from=builder /wheels /tmp/wheels
ENV PIP_BREAK_SYSTEM_PACKAGES=1
RUN pip3 install --no-cache-dir --no-index /tmp/wheels/*.whl \
    --ignore-installed blinker && \
    rm -rf /tmp/wheels

# The dedicated patched binary is used only for Plex loudness analysis.
COPY --from=ffmpeg-loudness-builder /opt/ffmpeg-loudness /opt/ffmpeg-loudness
RUN /opt/ffmpeg-loudness/bin/ffmpeg -version >/dev/null

# Loaded only by the credit text detection helper process (markers/credits/textdet_helper.py).
COPY --from=model /models/ch_PP-OCRv4_det_infer.onnx /app/models/ch_PP-OCRv4_det_infer.onnx
COPY --from=model /models/latin_PP-OCRv5_rec_mobile.onnx /app/models/latin_PP-OCRv5_rec_mobile.onnx

# Replace init-adduser with clean version (no branding)
COPY docker-init-user.sh /etc/s6-overlay/s6-rc.d/init-adduser/run
RUN chmod +x /etc/s6-overlay/s6-rc.d/init-adduser/run

# /dev/dri/by-path/ fixup — Intel NEO (OpenCL) enumerates GPUs only via
# /dev/dri/by-path, and NVIDIA Container Toolkit populates that directory
# only for NVIDIA cards.  On mixed Intel+NVIDIA containers the Intel GPU
# is invisible to OpenCL without this (VAAPI still works).  Idempotent /
# no-op on single-vendor and bare-metal setups.
COPY docker-init-dri-by-path.sh /etc/s6-overlay/s6-rc.d/init-dri-by-path/run
RUN chmod +x /etc/s6-overlay/s6-rc.d/init-dri-by-path/run && \
    echo oneshot > /etc/s6-overlay/s6-rc.d/init-dri-by-path/type && \
    echo /etc/s6-overlay/s6-rc.d/init-dri-by-path/run \
        > /etc/s6-overlay/s6-rc.d/init-dri-by-path/up && \
    mkdir -p /etc/s6-overlay/s6-rc.d/init-dri-by-path/dependencies.d && \
    touch /etc/s6-overlay/s6-rc.d/init-dri-by-path/dependencies.d/init-adduser && \
    touch /etc/s6-overlay/s6-rc.d/init-device-perms/dependencies.d/init-dri-by-path && \
    touch /etc/s6-overlay/s6-rc.d/user/contents.d/init-dri-by-path

# Set working directory
WORKDIR /app

# Build metadata (optional; set via --build-arg in CI). Declared here, after the apt layers, because every RUN after
# an ARG is rebuilt when its value changes, and GIT_SHA changes every commit.
ARG GIT_BRANCH=unknown
ARG GIT_SHA=unknown
ARG BUILD_DATE=unknown

# Published image name baked in at build time. CI passes one of:
#   - stevezzau/plex_generate_vid_previews  (deprecated mirror; banner fires)
#   - stevezzau/media_preview_generator     (canonical name; no banner)
#   - local                                  (dev / unset; no banner)
# The runtime reads $DOCKER_IMAGE_NAME and surfaces an in-app deprecation
# notification when it matches the deprecated name.
ARG DOCKER_IMAGE_NAME=local

# Expose build metadata to the app (non-secret)
ENV GIT_BRANCH=${GIT_BRANCH} \
    GIT_SHA=${GIT_SHA} \
    BUILD_DATE=${BUILD_DATE} \
    DOCKER_IMAGE_NAME=${DOCKER_IMAGE_NAME}

# Copy application source (needed for Flask templates and static files)
COPY pyproject.toml ./
COPY media_preview_generator/ ./media_preview_generator/
# Retain the filtered detector source for the Reprise integration; do not install its obsolete requirements.
COPY third_party/recurring-content-detector/ ./third_party/recurring-content-detector/

# Copy wrapper script
COPY wrapper.sh /app/wrapper.sh
RUN chmod +x /app/wrapper.sh

# Persist build metadata as a JSON artifact too — startup logs read this
# alongside the env vars so a tag-drift incident is grep-able from
# `docker logs` ("Build: ..."). Falls back gracefully when build args
# aren't supplied.
RUN printf '{"branch": "%s", "sha": "%s", "built": "%s"}\n' \
        "${GIT_BRANCH}" "${GIT_SHA}" "${BUILD_DATE}" \
        > /app/build_info.json

# Default PUID/PGID (override with environment variables)
ENV PUID=1000 \
    PGID=1000 \
    ATTACHED_DEVICES_PERMS="/dev/dri -type c"

# Tell s6-overlay to preserve environment variables
ENV S6_KEEP_ENV=1

# Web UI port only. A node (MODE=node) is published with -p 9494:9494 instead: with a second exposed port, reverse
# proxies that route to a container's only exposed port (nginx-proxy given just VIRTUAL_HOST) pick port 80 -> 502.
EXPOSE 8080

# Health check for the web UI (WEB_PORT), or for a node's /v1/health (NODE_PORT) when MODE=node
# Uses python3 stdlib instead of curl (one fewer binary dependency).
# exec replaces the shell so Docker kills the Python process on timeout (no orphans).
# timeout=5 ensures the process exits if the server is unresponsive.
# `or`, not get()'s default: an empty NODE_PORT/WEB_PORT is the default port. A node reads its port and host as the
# node's server does (nodes/server/wsgi.py): FILE__NODE_PORT's file, else NODE_PORT, else FILE__AGENT_PORT's file,
# else AGENT_PORT; FILE__NODE_HOST's file, else NODE_HOST. It is asked where it listens: with --network host,
# NODE_HOST may be one address only. Waitress takes :: as IPv6 only, so that one is asked on ::1.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD exec python3 -c "import urllib.request, os; e = os.environ; node = (e.get('MODE') or '').strip().lower() == 'node'; read = lambda n: (open(e['FILE__' + n], encoding='utf-8-sig').read().strip() if node and e.get('FILE__' + n) else '') or e.get(n, '').strip(); port = read('NODE_PORT') or read('AGENT_PORT') or '9494'; host = read('NODE_HOST') if node else 'localhost'; host = {'': '127.0.0.1', '0.0.0.0': '127.0.0.1', '::': '::1'}.get(host, host); urllib.request.urlopen('http://' + ('[' + host + ']' if ':' in host else host) + ':' + (port + '/v1/health' if node else (e.get('WEB_PORT') or '8080') + '/api/health'), timeout=5)"

# Our own labels: without these the image shows linuxserver/ffmpeg's, inherited from the base (#428). Last, because
# GIT_SHA changes every commit and a changed LABEL rebuilds every layer after it.
ARG SETUPTOOLS_SCM_PRETEND_VERSION=""
LABEL maintainer="nphil" \
      build_version="Reprise ${SETUPTOOLS_SCM_PRETEND_VERSION} (${GIT_SHA})" \
      org.opencontainers.image.title="Reprise" \
      org.opencontainers.image.description="Self-hosted media analysis and review" \
      org.opencontainers.image.authors="nphil and contributors" \
      org.opencontainers.image.vendor="nphil" \
      org.opencontainers.image.licenses="MIT AND GPL-2.0-or-later AND LGPL-2.1-or-later" \
      org.opencontainers.image.url="https://github.com/nphil/reprise" \
      org.opencontainers.image.documentation="https://github.com/nphil/reprise" \
      org.opencontainers.image.source="https://github.com/nphil/reprise" \
      org.opencontainers.image.version="${SETUPTOOLS_SCM_PRETEND_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.ref.name="${GIT_BRANCH}" \
      org.opencontainers.image.created="${BUILD_DATE}"

# Use LinuxServer's /init for PUID/PGID handling
ENTRYPOINT ["/init", "/app/wrapper.sh"]

# Web UI only; configure via the browser at http://<host>:<port>
CMD []
