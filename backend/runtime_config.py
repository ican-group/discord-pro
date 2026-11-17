"""Runtime configuration validation for the BitBotPro backend (spec 004).

Standard library only, so the rules can be unit-tested without FastAPI,
MongoDB or network access. ``server.py`` calls these at import time and
refuses to start on invalid configuration.
"""
from __future__ import annotations

import re
from typing import List, Mapping, Tuple


class ConfigError(ValueError):
    """Raised for invalid runtime configuration. Messages never contain secrets."""


# ── APP_MODE ────────────────────────────────────────────────────────────────
#   all — API plus Discord bot (legacy/Emergent behaviour; default when unset)
#   api — API only: Discord bot processes and the MongoDB Knowledge Base are disabled
APP_MODE_ALL = "all"
APP_MODE_API = "api"
VALID_APP_MODES = (APP_MODE_ALL, APP_MODE_API)


def get_app_mode(env: Mapping[str, str]) -> str:
    raw = (env.get("APP_MODE") or APP_MODE_ALL).strip().lower()
    if raw not in VALID_APP_MODES:
        raise ConfigError(
            f"Invalid APP_MODE {raw!r}; expected one of: {', '.join(VALID_APP_MODES)}"
        )
    return raw


def discord_allowed(app_mode: str) -> bool:
    """Discord bot processes may only run outside API-only mode."""
    return app_mode != APP_MODE_API


# ── APP_ENV ─────────────────────────────────────────────────────────────────
APP_ENV_PRODUCTION = "production"
APP_ENV_DEVELOPMENT = "development"
VALID_APP_ENVS = (APP_ENV_PRODUCTION, APP_ENV_DEVELOPMENT)


def get_app_env(env: Mapping[str, str]) -> str:
    # Fail safe: unset means production (strictest rules).
    raw = (env.get("APP_ENV") or APP_ENV_PRODUCTION).strip().lower()
    if raw not in VALID_APP_ENVS:
        raise ConfigError(
            f"Invalid APP_ENV {raw!r}; expected one of: {', '.join(VALID_APP_ENVS)}"
        )
    return raw


# ── CORS ────────────────────────────────────────────────────────────────────
# Exact origins only: scheme://host[:port], lowercase, no path, no trailing slash.
_HTTPS_ORIGIN = re.compile(
    r"^https://(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*(?::[0-9]{1,5})?$"
)
_DEV_HTTP_ORIGIN = re.compile(r"^http://(?:localhost|127\.0\.0\.1)(?::[0-9]{1,5})?$")


def parse_cors_origins(raw: str | None, app_env: str) -> List[str]:
    """Return the validated list of allowed origins, or raise ConfigError.

    - CORS_ORIGINS is required; there is no default and no wildcard.
    - Production: only exact https:// origins.
    - Development: additionally http://localhost[:port] and http://127.0.0.1[:port].
    """
    if raw is None or not raw.strip():
        raise ConfigError("CORS_ORIGINS is required (comma-separated exact origins); no default is applied")

    origins: List[str] = []
    for part in raw.split(","):
        origin = part.strip()
        if not origin:
            raise ConfigError("CORS_ORIGINS contains an empty entry")
        if "*" in origin:
            raise ConfigError("CORS_ORIGINS must not contain wildcards")
        if origin.lower() == "null":
            raise ConfigError("CORS_ORIGINS must not contain 'null'")
        if origin != origin.lower():
            raise ConfigError(f"CORS origin must be lowercase: {origin!r}")
        if _HTTPS_ORIGIN.match(origin):
            _check_port(origin)
        elif _DEV_HTTP_ORIGIN.match(origin):
            if app_env != APP_ENV_DEVELOPMENT:
                raise ConfigError(
                    f"CORS origin {origin!r} is only allowed when APP_ENV=development"
                )
            _check_port(origin)
        else:
            raise ConfigError(
                f"Invalid CORS origin {origin!r}: expected an exact https://host[:port] "
                "origin with no path or trailing slash"
            )
        if origin not in origins:
            origins.append(origin)
    return origins


def _check_port(origin: str, label: str = "CORS origin") -> None:
    host_part = origin.split("://", 1)[1]
    if ":" in host_part:
        port = int(host_part.rsplit(":", 1)[1])
        if not 1 <= port <= 65535:
            raise ConfigError(f"{label} has an invalid port: {origin!r}")


# ── Public URLs (spec 008, R-P6.1) ──────────────────────────────────────────
#   PUBLIC_APP_URL — user-facing frontend origin (invitations, email links,
#                    OAuth landing pages, widget iframe), e.g. https://bitbotpro.io
#   PUBLIC_API_URL — API origin for webhooks and OAuth callbacks,
#                    e.g. https://api.bitbotpro.io
# Validation only: Stage 1 does not read these in any route, and the compose
# file does not set them yet. Activation (required=True) happens in later stages.
PUBLIC_APP_URL = "PUBLIC_APP_URL"
PUBLIC_API_URL = "PUBLIC_API_URL"


def parse_public_url(name: str, raw: str | None, app_env: str, *, required: bool = False) -> str | None:
    """Validate a public origin: exact ``scheme://host[:port]``, lowercase, no
    path, query, fragment, credentials or trailing slash.

    - Production: ``https://`` only.
    - Development: additionally ``http://localhost[:port]`` / ``http://127.0.0.1[:port]``.
    - Unset/blank: ``None``, unless ``required`` (then ConfigError).
    """
    if raw is None or not raw.strip():
        if required:
            raise ConfigError(f"{name} is required")
        return None
    value = raw.strip()
    if value != value.lower():
        raise ConfigError(f"{name} must be lowercase: {value!r}")
    if _HTTPS_ORIGIN.match(value):
        _check_port(value, name)
        return value
    if _DEV_HTTP_ORIGIN.match(value):
        if app_env != APP_ENV_DEVELOPMENT:
            raise ConfigError(f"{name} must be an https:// origin unless APP_ENV=development: {value!r}")
        _check_port(value, name)
        return value
    raise ConfigError(
        f"Invalid {name} {value!r}: expected an exact https://host[:port] origin with no path, "
        "query, credentials or trailing slash"
    )


def public_url(name: str, env: Mapping[str, str] | None = None) -> str:
    """Current value of PUBLIC_APP_URL / PUBLIC_API_URL ("" when unset).

    server.py validates both at import (required when APP_ENV=production), so
    route modules can read them at call time without re-validating."""
    import os as _os
    source = _os.environ if env is None else env
    return (source.get(name) or "").strip().rstrip("/")


def get_public_urls(env: Mapping[str, str], app_env: str, *, required: bool = False) -> Tuple[str | None, str | None]:
    """Return ``(PUBLIC_APP_URL, PUBLIC_API_URL)``, validated."""
    app_url = parse_public_url(PUBLIC_APP_URL, env.get(PUBLIC_APP_URL), app_env, required=required)
    api_url = parse_public_url(PUBLIC_API_URL, env.get(PUBLIC_API_URL), app_env, required=required)
    return app_url, api_url


# ── Knowledge Base (MongoDB) ────────────────────────────────────────────────
# The MongoDB Knowledge Base is disabled in API-only mode until the
# PostgreSQL/pgvector migration (spec 005) lands.
KB_PATH_PREFIX = "/api/knowledge"
KB_UNAVAILABLE_DETAIL = (
    "The Knowledge Base is temporarily unavailable pending migration to "
    "PostgreSQL/pgvector."
)


def kb_disabled(app_mode: str) -> bool:
    return app_mode == APP_MODE_API


def is_kb_path(path: str) -> bool:
    """True for /api/knowledge and anything beneath it."""
    return path == KB_PATH_PREFIX or path.startswith(KB_PATH_PREFIX + "/")


# ── Emergent object storage ─────────────────────────────────────────────────
def emergent_storage_configured(env: Mapping[str, str]) -> bool:
    return bool((env.get("CLAUDE_API_KEY") or env.get("EMERGENT_LLM_KEY") or "").strip())


# ── Code HMAC key (spec 008, R-P2.3, A3) ────────────────────────────────────
# Keyed hashing of verification/reset codes and of account identifiers used as
# rate-limit keys. Supplied as a Docker secret (CODE_HMAC_KEY_FILE → entrypoint).
CODE_HMAC_KEY = "CODE_HMAC_KEY"
CODE_HMAC_KEY_MIN_LENGTH = 32


def get_code_hmac_key(env: Mapping[str, str], app_env: str, *, jwt_secret: str | None = None) -> Tuple[bytes, bool]:
    """Return ``(key_bytes, ephemeral)``.

    - Production: CODE_HMAC_KEY is required (fail closed), at least 32
      characters, and must differ from JWT_SECRET (key separation).
    - Development: if unset, a random per-process key is generated
      (``ephemeral=True``); codes issued before a restart then stop working.
    Error messages never contain the key.
    """
    raw = (env.get(CODE_HMAC_KEY) or "").strip()
    if not raw:
        if app_env == APP_ENV_DEVELOPMENT:
            import os as _os
            return _os.urandom(32), True
        raise ConfigError(f"{CODE_HMAC_KEY} is required when APP_ENV={app_env} (set {CODE_HMAC_KEY}_FILE)")
    if len(raw) < CODE_HMAC_KEY_MIN_LENGTH:
        raise ConfigError(f"{CODE_HMAC_KEY} must be at least {CODE_HMAC_KEY_MIN_LENGTH} characters")
    if jwt_secret is not None and raw == jwt_secret.strip():
        raise ConfigError(f"{CODE_HMAC_KEY} must differ from JWT_SECRET")
    return raw.encode("utf-8"), False


# ── Registration policy (spec 008, P8 / DD1) ────────────────────────────────
REGISTRATION_MODE = "REGISTRATION_MODE"
REGISTRATION_INVITE_ONLY = "invite_only"
REGISTRATION_OPEN = "open"
VALID_REGISTRATION_MODES = (REGISTRATION_INVITE_ONLY, REGISTRATION_OPEN)


def get_registration_mode(env: Mapping[str, str], app_env: str) -> str:
    """`invite_only` (production default and the only value allowed in production
    for this release, R-P8.4) or `open` (development default)."""
    default = REGISTRATION_INVITE_ONLY if app_env == APP_ENV_PRODUCTION else REGISTRATION_OPEN
    raw = (env.get(REGISTRATION_MODE) or default).strip().lower()
    if raw not in VALID_REGISTRATION_MODES:
        raise ConfigError(f"Invalid {REGISTRATION_MODE} {raw!r}; expected one of: {', '.join(VALID_REGISTRATION_MODES)}")
    if raw == REGISTRATION_OPEN and app_env == APP_ENV_PRODUCTION:
        raise ConfigError(f"{REGISTRATION_MODE}=open is not allowed in production (invite-only launch)")
    return raw


def get_auth_response_floor_ms(env: Mapping[str, str]) -> int:
    """Minimum response time for account-related endpoints (S1 timing), in ms."""
    raw = (env.get("AUTH_RESPONSE_FLOOR_MS") or "400").strip()
    if not raw.isdigit() or int(raw) > 5000:
        raise ConfigError("AUTH_RESPONSE_FLOOR_MS must be an integer between 0 and 5000")
    return int(raw)
