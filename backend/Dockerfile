# syntax=docker/dockerfile:1
# BitBotPro backend API image (spec 004).
#
# Stages:
#   builder  - virtualenv with requirements.txt (build tools live only here)
#   test     - runs the offline unit tests against the real dependencies
#              (docker build --target test ...); never shipped
#   runtime  - minimal, non-root image (default target)
#
# No secrets are used at build time: no secret ARG/ENV, no .env files, and the
# build context is restricted by .dockerignore (allowlist).

ARG PYTHON_IMAGE=python:3.11.18-slim-bookworm

# ── builder ─────────────────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS builder
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt \
 && pip check

# ── test (optional target) ──────────────────────────────────────────────────
FROM builder AS test
WORKDIR /src
COPY requirements.txt *.py docker-entrypoint.sh ./
COPY zendesk_sidebar_app/assets ./zendesk_sidebar_app/assets
COPY tests/unit ./tests/unit
# Real-MongoDB integration tests (spec 008 Stage 3). Not run during the build:
# deploy/scripts/run-stage3-integration-tests.sh runs them against a disposable
# mongod on an isolated internal network.
COPY tests/integration ./tests/integration
RUN python -m unittest discover -s tests/unit -t . -v

# ── runtime ─────────────────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS runtime
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home \
            --home-dir /nonexistent --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
# Application code is owned by root and read-only for the runtime user.
COPY *.py docker-entrypoint.sh ./
COPY zendesk_sidebar_app/assets ./zendesk_sidebar_app/assets
RUN chmod 0755 /app/docker-entrypoint.sh \
 && chmod -R a+rX,go-w /app \
 && python -m compileall -q /app
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
# One worker: chat sessions are held in process memory (spec 004, F3).
# --no-proxy-headers: request.client is always the real TCP peer; forwarded-IP
# headers are never trusted by uvicorn (spec 008, R-IP.3). Client IPs are
# resolved only by client_ip.py.
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--timeout-graceful-shutdown", "25", "--no-server-header", "--no-proxy-headers"]
