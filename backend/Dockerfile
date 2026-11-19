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

# Pinned by digest (Phase 6C): python:3.11.18-slim-trixie
ARG PYTHON_IMAGE=python:3.11.18-slim-trixie@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

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
 && pip check \
 && pip uninstall -y setuptools wheel

# ── test (optional target) ──────────────────────────────────────────────────
FROM builder AS test
WORKDIR /src
COPY requirements.txt *.py docker-entrypoint.sh ./
COPY zendesk_sidebar_app/assets ./zendesk_sidebar_app/assets
COPY tests/unit ./tests/unit
# Spec 012 packages (core, persistence, API, runtime). Since Phase 6B the runtime image is the
# Spec 012 release candidate and ships them too (explicit list in the runtime stage).
COPY core ./core
COPY api ./api
COPY persistence ./persistence
COPY runtime ./runtime
# Real-MongoDB integration tests (spec 008 Stage 3). Not run during the build:
# deploy/scripts/run-stage3-integration-tests.sh runs them against a disposable
# mongod on an isolated internal network.
COPY tests/integration ./tests/integration
# Phase 5/6A tooling (test target only): frontend contract export, knowledge evaluation harness,
# local end-to-end server. Never in the runtime image.
COPY tests/contract ./tests/contract
COPY tests/knowledge_eval ./tests/knowledge_eval
COPY tests/e2e ./tests/e2e
RUN python -m unittest discover -s tests/unit -t . -v

# ── runtime ─────────────────────────────────────────────────────────────────
# Spec 012 release candidate (Phase 6B): the API (APP_MODE=api, default command) and the
# worker (APP_MODE=worker, command ["python", "worker.py"]) run from this one image (D-Q4).
# Only the Spec 012 packages and the kept modules they import are copied (explicit list):
# the retained, unmounted Spec 011 API-integration sources and manage.py are not shipped.
FROM ${PYTHON_IMAGE} AS runtime
ARG SOURCE_TREE=unknown
LABEL org.opencontainers.image.title="bitbotpro-api" \
      org.opencontainers.image.description="BitBotPro Spec 012 API and worker" \
      org.opencontainers.image.revision="${SOURCE_TREE}" \
      io.bitbotpro.spec="012" \
      io.bitbotpro.source-tree="${SOURCE_TREE}"
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    APP_MODE=api
# Phase 6B vulnerability review: the base image's own packaging tools are not needed at run time.
# Phase 6C: the one Debian fix published for the remaining high findings (zlib1g, deb13u1), by exact version.
RUN pip uninstall -y --root-user-action=ignore setuptools wheel || true \
 && apt-get update \
 && apt-get install -y --no-install-recommends --only-upgrade zlib1g=1:1.3.dfsg+really1.3.1-1+deb13u1 \
 && rm -rf /var/lib/apt/lists/*
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home \
            --home-dir /nonexistent --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
# Application code is owned by root and read-only for the runtime user.
COPY server.py worker.py healthcheck.py docker-entrypoint.sh \
     client_ip.py email_delivery.py log_redaction.py outbound_guard.py password_policy.py \
     rate_limit.py runtime_config.py security_utils.py webhook_auth.py ./
COPY core ./core
COPY api ./api
COPY persistence ./persistence
COPY runtime ./runtime
COPY zendesk_sidebar_app/assets ./zendesk_sidebar_app/assets
RUN chmod 0755 /app/docker-entrypoint.sh \
 && chmod -R a+rX,go-w /app \
 && python -m compileall -q /app
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 CMD ["python", "/app/healthcheck.py"]
ENTRYPOINT ["/app/docker-entrypoint.sh"]
# One process: request throttles and sessions live in MongoDB; the worker is a separate container.
# --no-proxy-headers: request.client is always the real TCP peer; forwarded-IP headers are
# never trusted by uvicorn (spec 008, R-IP.3). Client IPs are resolved only by client_ip.py
# (TRUSTED_PROXIES + CF-Connecting-IP from the Caddy edge).
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--timeout-graceful-shutdown", "25", "--no-server-header", "--no-proxy-headers"]
