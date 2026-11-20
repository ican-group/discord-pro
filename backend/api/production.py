"""Production composition root for the Spec 012 API and worker (Phase 6B).

Reads and validates the environment once, builds the real adapters (MongoDB,
key ring, Anthropic/OpenAI over HTTPS, Telegram/Discord/Zendesk clients with no
test transports, the Resend mailer) and assembles the FastAPI app or the worker.
``server.py`` (API) and ``worker.py`` (``APP_MODE=worker``) are thin wiring on
top of this module (spec §2: "server.py becomes app wiring only").

Nothing here contacts an external service at import or start-up, except
MongoDB. Configuration errors stop the process with a message that names the
variable, never its value.

Environment (see spec §32.6 for the full audit):

| Variable | Required | Secret | Meaning |
|---|---|---|---|
| ``APP_MODE`` | yes | no | ``api`` (this app) or ``worker`` |
| ``APP_ENV`` | no (``production``) | no | ``production`` or ``development`` |
| ``MONGO_URL`` / ``_FILE`` | yes | **yes** | MongoDB connection string |
| ``DB_NAME`` | yes | no | the Spec 012 database (never a legacy database: refused at start) |
| ``JWT_SECRET`` / ``_FILE`` | yes | **yes** | session signing key, ≥ 32 characters |
| ``ENCRYPTION_KEYS`` / ``_FILE`` + ``ENCRYPTION_ACTIVE_KEY_ID`` | one of | **yes** | versioned key ring |
| ``ENCRYPTION_KEY`` / ``_FILE`` (+ ``ENCRYPTION_KEY_ID``) | one of | **yes** | single key (id ``k1``) |
| ``PUBLIC_API_URL`` | yes | no | ``https://api.bitbotpro.io``: webhooks, OAuth callbacks, embed.js, widget |
| ``PUBLIC_APP_URL`` | yes | no | ``https://bitbotpro.io``: reset/invitation links, OAuth result pages |
| ``CORS_ORIGINS`` | no | no | exact https origins of the admin app (credentials CORS) |
| ``ALLOWED_HOSTS`` | no | no | Host header allowlist; default: the PUBLIC_API_URL host + loopback |
| ``TRUSTED_PROXIES`` | no | no | peers whose ``CF-Connecting-IP`` is trusted (Caddy: 10.50.0.1) |
| ``RESEND_API_KEY`` / ``_FILE`` | no | **yes** | e-mail delivery; without it no e-mail is sent |
| ``FROM_EMAIL`` | with Resend | no | sender address |
| ``WEBCHAT_DAILY_LIMIT_PER_INSTANCE`` | no (2000) | no | W7 per-instance daily cap |
| ``DISCORD_GATEWAY_URL`` | worker, no | no | default ``wss://gateway.discord.gg`` |
| ``WORKER_INTERVAL_SECONDS`` | worker, no (15) | no | worker pass interval |
| ``WORKER_ID`` | worker, no | no | lease owner id (default: hostname) |
"""
from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple
from urllib.parse import urlsplit

log = logging.getLogger("bitbotpro")

APP_MODES = ("api", "worker")
APP_ENVS = ("production", "development")
DEFAULT_GATEWAY_URL = "wss://gateway.discord.gg"
HEARTBEAT_FILE = "/tmp/bbp-worker-heartbeat"


class ProductionConfigError(ValueError):
    """A configuration problem. Names the variable, never its value."""


def _origin(name: str, raw: Optional[str], *, app_env: str, required: bool) -> Optional[str]:
    raw = (raw or "").strip()
    if not raw:
        if required:
            raise ProductionConfigError(f"{name} is required.")
        return None
    parts = urlsplit(raw)
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.path not in ("", "/") or parts.query \
            or parts.fragment or parts.username or parts.password:
        raise ProductionConfigError(f"{name} must be an origin such as https://host (no path, query or credentials).")
    if parts.scheme != "https" and app_env != "development":
        raise ProductionConfigError(f"{name} must use https:// unless APP_ENV=development.")
    return f"{parts.scheme}://{parts.netloc}"


@dataclass(frozen=True)
class ProductionConfig:
    app_mode: str
    app_env: str
    mongo_url: str = field(repr=False)
    db_name: str
    jwt_secret: str = field(repr=False)
    key_ring: Any = field(repr=False)
    public_api_origin: str
    public_app_origin: str
    admin_origins: Tuple[str, ...]
    allowed_hosts: Tuple[str, ...]
    trusted_proxies: str
    resend_api_key: str = field(repr=False, default="")
    from_email: str = ""
    webchat_daily_limit: int = 2000
    gateway_url: str = DEFAULT_GATEWAY_URL
    worker_interval: float = 15.0
    worker_id: str = ""

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> "ProductionConfig":
        from core.credentials import KeyRing, KeyRingError
        mode = (env.get("APP_MODE") or "").strip().lower()
        if mode not in APP_MODES:
            raise ProductionConfigError("APP_MODE must be 'api' or 'worker'.")
        app_env = (env.get("APP_ENV") or "production").strip().lower()
        if app_env not in APP_ENVS:
            raise ProductionConfigError("APP_ENV must be 'production' or 'development'.")
        mongo_url = (env.get("MONGO_URL") or "").strip()
        if not mongo_url:
            raise ProductionConfigError("MONGO_URL is required.")
        db_name = (env.get("DB_NAME") or "").strip()
        if not db_name or not all(c.isalnum() or c in "_-" for c in db_name) or len(db_name) > 63:
            raise ProductionConfigError("DB_NAME is required (letters, digits, '_' and '-').")
        jwt_secret = env.get("JWT_SECRET") or ""
        if len(jwt_secret) < 32:
            raise ProductionConfigError("JWT_SECRET is required (at least 32 characters).")
        try:
            ring = KeyRing.from_environment(env)
        except KeyRingError as exc:
            raise ProductionConfigError(str(exc)) from None
        api = _origin("PUBLIC_API_URL", env.get("PUBLIC_API_URL"), app_env=app_env, required=True)
        app = _origin("PUBLIC_APP_URL", env.get("PUBLIC_APP_URL"), app_env=app_env, required=True)
        cors = []
        for raw in (env.get("CORS_ORIGINS") or "").split(","):
            if raw.strip():
                o = _origin("CORS_ORIGINS", raw, app_env=app_env, required=True)
                if not o.startswith("https://"):
                    raise ProductionConfigError("CORS_ORIGINS entries must be https:// origins.")
                cors.append(o)
        hosts = tuple(h.strip().lower() for h in (env.get("ALLOWED_HOSTS") or "").split(",") if h.strip())
        if not hosts:
            hosts = (urlsplit(api).hostname, "127.0.0.1", "localhost")
        if any(h == "*" or "/" in h or " " in h for h in hosts):
            raise ProductionConfigError("ALLOWED_HOSTS must list exact host names (no wildcard).")
        resend = (env.get("RESEND_API_KEY") or "").strip()
        from_email = (env.get("FROM_EMAIL") or "").strip()
        if resend and "@" not in from_email:
            raise ProductionConfigError("FROM_EMAIL is required when RESEND_API_KEY is set.")
        try:
            daily = int(env.get("WEBCHAT_DAILY_LIMIT_PER_INSTANCE") or 2000)
            interval = float(env.get("WORKER_INTERVAL_SECONDS") or 15)
        except ValueError:
            raise ProductionConfigError("WEBCHAT_DAILY_LIMIT_PER_INSTANCE / WORKER_INTERVAL_SECONDS must be numbers.")
        if not 1 <= daily <= 1_000_000 or not 1 <= interval <= 300:
            raise ProductionConfigError("WEBCHAT_DAILY_LIMIT_PER_INSTANCE or WORKER_INTERVAL_SECONDS out of range.")
        gateway = (env.get("DISCORD_GATEWAY_URL") or DEFAULT_GATEWAY_URL).strip().rstrip("/")
        if not gateway.startswith("wss://"):
            raise ProductionConfigError("DISCORD_GATEWAY_URL must be a wss:// URL.")
        return cls(app_mode=mode, app_env=app_env, mongo_url=mongo_url, db_name=db_name, jwt_secret=jwt_secret,
                   key_ring=ring, public_api_origin=api, public_app_origin=app, admin_origins=tuple(cors),
                   allowed_hosts=hosts, trusted_proxies=(env.get("TRUSTED_PROXIES") or "").strip(),
                   resend_api_key=resend, from_email=from_email, webchat_daily_limit=daily, gateway_url=gateway,
                   worker_interval=interval, worker_id=(env.get("WORKER_ID") or socket.gethostname())[:64])

    def settings(self):
        from api.common import Settings
        app = self.public_app_origin
        return Settings(jwt_secret=self.jwt_secret, encryption_key=self.key_ring,
                        cookie_secure=self.app_env != "development" or app.startswith("https://"),
                        reset_url_base=f"{app}/app/reset-password#token=",
                        invitation_url_base=f"{app}/app/invitations#token=",
                        public_api_origin=self.public_api_origin,
                        webchat_daily_limit_per_instance=self.webchat_daily_limit,
                        oauth_result_origin=app, admin_origins=self.admin_origins)


class ResendMailer:
    """Production mailer (Phase 6B): the kept ``email_delivery`` service over
    Resend. Without ``RESEND_API_KEY`` nothing is sent and callers see a failure
    (invitations report ``email_sent: false``; password reset answers the same
    202 as always). Links and tokens are never logged."""

    def __init__(self, config: ProductionConfig, *, send_fn=None):
        import email_delivery
        self._cfg = config
        if send_fn is None:
            def send_fn(params):
                import resend
                resend.api_key = config.resend_api_key
                return resend.Emails.send(params)
        self.service = email_delivery.EmailService(app_env="production", api_key_fn=lambda: config.resend_api_key,
                                                   send_fn=send_fn, from_email=config.from_email or "noreply@invalid")

    async def _send(self, kind: str, to: str, subject: str, html: str) -> None:
        await self.service.deliver(kind, to, subject, html)

    async def send_password_reset(self, email: str, reset_url: str) -> None:
        from html import escape
        await self._send("password_reset", email, "Reset your BitBotPro password",
                         f"<p>Use this link to choose a new password. It expires soon and works once.</p>"
                         f"<p><a href=\"{escape(reset_url, quote=True)}\">Reset password</a></p>"
                         f"<p>If you did not ask for this, ignore this e-mail.</p>")

    async def send_invitation(self, email: str, *, subject: str, text: str, html: str) -> None:
        await self._send("invitation", email, subject, html)

    async def send_discord_invite(self, email: str, install_url: str, *, organisation_name: str,
                                  instance_name: str, expires_at: str) -> None:
        from html import escape
        await self._send("discord_invite", email, "Add the BitBotPro bot to your Discord server",
                         f"<p>{escape(organisation_name)} invites you to add the bot for "
                         f"<strong>{escape(instance_name)}</strong> to your Discord server.</p>"
                         f"<p><a href=\"{escape(install_url, quote=True)}\">Add to Discord</a> "
                         f"(expires {escape(expires_at)}).</p>")


def build_services(config: ProductionConfig, db):
    """Services with the real adapters: no test transport, resolver or provider."""
    from api.common import Services
    from persistence.mongo import MongoStore
    from runtime.providers import build_providers
    S = Services(MongoStore(db), config.settings(), mailer=ResendMailer(config))
    S.providers = build_providers()
    # transports/resolver stay None: the real Telegram, Discord and Zendesk endpoints, every Zendesk
    # call through the kept outbound_guard (real DNS, pinned address, no redirects)
    return S


def configure_client_ip(config: ProductionConfig) -> None:
    import client_ip
    client_ip.configure(client_ip.ClientIPResolver(client_ip.parse_trusted_proxies(config.trusted_proxies or None)))


def create_api_app(config: ProductionConfig, *, client=None):
    """The production FastAPI app. The database is prepared (and a legacy or
    foreign database refused) before the first request is served."""
    from contextlib import asynccontextmanager
    from fastapi.responses import JSONResponse
    from motor.motor_asyncio import AsyncIOMotorClient
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    from api.app import create_app
    from persistence.mongo import prepare_database

    if config.app_mode != "api":
        raise ProductionConfigError("APP_MODE must be 'api' for the API process.")
    configure_client_ip(config)
    client = client or AsyncIOMotorClient(config.mongo_url, serverSelectionTimeoutMS=5000, appname="bitbotpro-api")
    db = client[config.db_name]
    S = build_services(config, db)
    app = create_app(S)
    app.state.ready = False

    @asynccontextmanager
    async def lifespan(_app):
        import log_redaction
        log_redaction.install()             # uvicorn's handlers exist only now (access log included)
        await prepare_database(db)          # DatabaseGuardError stops the process: never serve a legacy DB
        _app.state.ready = True
        log.info("bitbotpro api ready (db=%s, keys=%s, active=%s)", config.db_name,
                 ",".join(config.key_ring.key_ids), config.key_ring.active)
        try:
            yield
        finally:
            client.close()

    app.router.lifespan_context = lifespan

    @app.get("/api/health", include_in_schema=False)
    async def health():
        """Liveness for Docker and Caddy: no database detail, no version string."""
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})

    @app.get("/api/ready", include_in_schema=False)
    async def ready():
        try:
            await db.command("ping")
            ok = bool(app.state.ready)
        except Exception:
            ok = False
        return JSONResponse({"status": "ready" if ok else "unavailable"}, status_code=200 if ok else 503,
                            headers={"Cache-Control": "no-store"})

    # outermost: a request with a foreign Host header is refused before routing (400)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(config.allowed_hosts))
    app.state.services = S
    return app


async def run_worker(config: ProductionConfig, *, stop=None, client=None, heartbeat_file: str = HEARTBEAT_FILE):
    """The long-running worker (D-Q4): deletion jobs, binding reconciliation,
    deliveries, Telegram webhook sync, Zendesk maintenance, knowledge ingestion
    and the Discord gateway (one connection per enabled Enterprise application,
    D-Q14). Never started by the API process."""
    import asyncio
    import pathlib
    import time
    from motor.motor_asyncio import AsyncIOMotorClient
    from api.discord import channel_for as discord_channel
    from api.integrations import service_for
    from api.telegram import channel_for as telegram_channel
    from persistence.mongo import prepare_database
    from runtime.discord import discord_cleanup_executors
    from runtime.discord_gateway import GatewayManager
    from runtime.discord_gateway_ws import connection_factory
    from runtime.telegram import telegram_cleanup_executors
    from runtime.worker import Worker
    from runtime.zendesk import zendesk_cleanup_executors

    if config.app_mode != "worker":
        raise ProductionConfigError("APP_MODE must be 'worker' for the worker process.")
    client = client or AsyncIOMotorClient(config.mongo_url, serverSelectionTimeoutMS=5000, appname="bitbotpro-worker")
    db = client[config.db_name]
    try:
        await prepare_database(db)
        S = build_services(config, db)
        dc = discord_channel(S)
        gateway = GatewayManager(S.store, box=S.box, channel=dc, clock=S.clock, worker_id=config.worker_id,
                                 factory=connection_factory(gateway_url=config.gateway_url, store=S.store))
        executors = {**telegram_cleanup_executors(S.box), **discord_cleanup_executors(S.box),
                     **zendesk_cleanup_executors(S.box)}
        worker = Worker(S.store, clock=S.clock, worker_id=config.worker_id, executors=executors,
                        telegram=telegram_channel(S), discord=dc, gateway=gateway, zendesk=service_for(S))
        stop = stop or asyncio.Event()
        beat = pathlib.Path(heartbeat_file)

        async def heartbeat():
            while not stop.is_set():
                try:
                    beat.write_text(str(int(time.time())))
                except OSError:
                    pass
                try:
                    await asyncio.wait_for(stop.wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass

        # website knowledge imports (R1/R2): their own loop, so a long crawl never delays the rest
        import os as _os
        from runtime.website import Fetcher, WebsiteImporter
        websites = WebsiteImporter(S.store, clock=S.clock, fetcher=Fetcher(dict(_os.environ)))

        log.info("bitbotpro worker started (id=%s, db=%s)", config.worker_id, config.db_name)
        hb = asyncio.create_task(heartbeat())
        web_task = asyncio.create_task(websites.run_forever(stop))
        try:
            await worker.run_forever(stop, interval_seconds=config.worker_interval)
        finally:
            hb.cancel()
            web_task.cancel()
            await gateway.shutdown()
    finally:
        client.close()


def worker_healthy(heartbeat_file: str = HEARTBEAT_FILE, *, max_age: float = 120.0) -> bool:
    import time
    try:
        return time.time() - int(open(heartbeat_file).read().strip()) <= max_age
    except (OSError, ValueError):
        return False


__all__ = ["ProductionConfig", "ProductionConfigError", "ResendMailer", "build_services", "create_api_app",
           "run_worker", "worker_healthy"]
