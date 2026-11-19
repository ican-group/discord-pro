"""Webchat channel (spec 012 §5, §10, §11; Phase 4B; parity W1–W12).

Public routes (no account session; the instance comes only from its
server-assigned ``public_id`` resolved through the binding claims):

* ``GET  /public/instances/{pid}``: public widget configuration (PUBLIC registry
  values only; no hierarchy ids, credentials or private settings)
* ``GET  /public/instances/{pid}/logo``: the bot logo (D-Q8)
* ``POST /public/instances/{pid}/sessions``: a new conversation session
  (``session_id`` + ``session_token``, the token returned once)
* ``POST /public/instances/{pid}/chat`` and ``/chat/stream`` (SSE: ``session``,
  ``token``*, ``done`` | ``error``)
* ``GET  /public/instances/{pid}/sessions/{sid}/history``
* ``OPTIONS`` preflight for the above
* ``GET  /embed.js``, ``GET /public/widget/{pid}``, ``/public/widget.js``,
  ``/public/widget.css``

Every failure to resolve an available, permitted instance gives the same 404
("Not found."): unknown id, suspended/archived/deleted ancestors, channel not
allowed by policy, public access disabled, origin not allowed.

Browser security model:
* The widget runs in an iframe served by this API. Its CSP ``frame-ancestors``
  is the instance's effective allowed origins (instance ∩ Enterprise policy),
  so the browser refuses to embed it elsewhere. The widget's own API calls
  are same-origin.
* Direct cross-origin calls must come from an allowed origin. CORS echoes
  exactly that origin, never ``*`` and never with credentials (no cookies are
  used). Unsafe requests to a restricted instance must carry an ``Origin``.
* Non-browser clients can forge ``Origin``. Origin checks are an embedding
  control, **not authentication**. Abuse is bounded by per-IP, per-session and
  per-instance limits and monthly quotas; conversations are protected by the
  session token.
"""
import asyncio
import re
import uuid
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from api import webchat_assets
from api.common import Services, load_access, translate
from client_ip import client_ip, rate_limit_key
from core import audit, resolver, throttle
from core.authz import Action
from core.credentials import enterprise_credentials
from core.hierarchy import COLLECTION, ChannelType, HierarchyError, Level, sniff_logo
from core.identity import User
from runtime import conversations as conv
from runtime.errors import RuntimeFailure
from runtime.interfaces import ChannelAddress, InboundMessage
from runtime.pipeline import Pipeline
from runtime.routing import resolve_instance

PUBLIC_ID = re.compile(r"pk_[A-Za-z0-9_-]{22,64}")
SESSION_HEADER = "x-chat-session-token"
LOGOS = "bot_logos"
MAX_MESSAGE_CHARS = 4000
LIMIT_CHAT_IP = (20, 60)                  # legacy W7
LIMIT_HISTORY_IP = (60, 60)
LIMIT_PUBLIC_READ_IP = (300, 60)
LIMIT_SESSION_CREATE_IP = (20, 3600)
LIMIT_PER_SESSION = (10, 60)
PREVIEW_RATE = (30, 3600)                  # F5: admin preview sessions per user per hour
MAX_USER_MESSAGES_PER_SESSION = 200
NOT_FOUND = "Not found."
UNCERTAIN = {"http_status": 409,
             "message": "This message could not be completed. Please send it again as a new message."}


class ChatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str
    session_id: Optional[str] = None
    client_message_id: Optional[str] = None


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str


def _nf() -> HTTPException:
    return HTTPException(404, NOT_FOUND)


def _sse(event: str, data: Dict[str, Any]) -> str:
    import json
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _chunks(text: str, size: int = 32):
    words, buf = re.split(r"(\s+)", text), ""
    for w in words:
        buf += w
        if len(buf) >= size:
            yield buf
            buf = ""
    if buf:
        yield buf


def register(router: APIRouter, S: Services, principal) -> None:
    api_origin = S.settings.public_api_origin.lower().rstrip("/")

    async def limit(request: Request, scope: str, rule: Tuple[int, int], ident: Optional[str] = None):
        key = throttle.throttle_key(scope, ident or rate_limit_key(client_ip(request)))
        d = await throttle.hit(S.store, key, limit=rule[0], window_seconds=rule[1], now=S.clock())
        if not d.allowed:
            raise HTTPException(429, "Too many requests. Please try again later.",
                                headers={"Retry-After": str(d.retry_after)})

    async def context(pid: str):
        """Resolve an *available, permitted* webchat instance or 404."""
        if not isinstance(pid, str) or not PUBLIC_ID.fullmatch(pid):
            raise _nf()
        try:
            iid = await resolve_instance(S.store, ChannelAddress(ChannelType.WEBCHAT, public_id=pid))
            chain = await S.hierarchy.load_chain(Level.BOT_INSTANCE, iid)
        except (RuntimeFailure, HierarchyError):
            raise _nf()
        if not chain.availability().available:
            raise _nf()
        creds = await enterprise_credentials(S.store, chain.enterprise_id)
        eff = resolver.resolve(chain, platform_policy=S.platform_policy, credentials=creds)
        if eff.runtime_blockers:
            raise _nf()
        return chain, eff

    def origin_for(request: Request, eff, *, unsafe: bool) -> Optional[str]:
        """The origin to echo in CORS, None for no-Origin requests, or 404."""
        raw = request.headers.get("origin")
        allowed = eff.values.get("channel.allowed_origins")          # frozenset or None (= any)
        if raw is None:
            if unsafe and allowed is not None:
                raise _nf()
            return None
        origin = raw.strip().lower().rstrip("/")
        if origin == api_origin or (allowed is None and origin != "null") or (allowed and origin in allowed):
            return raw.strip()
        raise _nf()

    def cors(resp: Response, origin: Optional[str]) -> Response:
        if origin:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Vary"] = "Origin"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    # ── preflight ──
    async def preflight(pid: str, request: Request):
        _, eff = await context(pid)
        origin = origin_for(request, eff, unsafe=True)
        resp = Response(status_code=204)
        cors(resp, origin)
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST"
        resp.headers["Access-Control-Allow-Headers"] = "content-type, x-chat-session-token"
        resp.headers["Access-Control-Max-Age"] = "600"
        return resp

    router.add_api_route("/public/instances/{pid}", translate(preflight), methods=["OPTIONS"],
                         include_in_schema=False, name="webchat_preflight_config")
    router.add_api_route("/public/instances/{pid}/{rest:path}", translate(preflight), methods=["OPTIONS"],
                         include_in_schema=False, name="webchat_preflight")

    # ── public configuration ──
    @router.get("/public/instances/{pid}")
    @translate
    async def public_config(pid: str, request: Request):
        await limit(request, "wc-read", LIMIT_PUBLIC_READ_IP)
        chain, eff = await context(pid)
        origin = origin_for(request, eff, unsafe=False)
        pv = eff.public_values()
        body = {"public_id": pid, "title": pv.get("widget.title") or chain.bot.name,
                "welcome_message": pv.get("widget.welcome_message") or "", "theme": dict(pv.get("widget.theme") or {}),
                "default_open": bool(pv.get("widget.default_open")), "custom_css_url": pv.get("widget.custom_css_url") or "",
                "logo_url": f"/api/public/instances/{pid}/logo" if chain.bot.logo else None, "streaming": True}
        resp = JSONResponse(body)
        resp.headers["Cache-Control"] = "public, max-age=60"
        return cors(resp, origin)

    @router.get("/public/instances/{pid}/logo")
    @translate
    async def public_logo(pid: str, request: Request):
        await limit(request, "wc-read", LIMIT_PUBLIC_READ_IP)
        chain, eff = await context(pid)
        origin = origin_for(request, eff, unsafe=False)
        doc = await S.store.get(LOGOS, chain.bot.id)
        if doc is None or doc.get("enterprise_id") != chain.enterprise_id:
            raise _nf()
        resp = Response(content=bytes(doc["data"]), media_type=doc["content_type"])
        resp.headers["Content-Security-Policy"] = "default-src 'none'"
        resp.headers["Cache-Control"] = "public, max-age=300"
        return cors(resp, origin)

    # ── sessions ──
    @router.post("/public/instances/{pid}/sessions", status_code=201)
    @translate
    async def new_session(pid: str, request: Request):
        chain, eff = await context(pid)
        origin = origin_for(request, eff, unsafe=True)
        await limit(request, "wc-session", LIMIT_SESSION_CREATE_IP)
        doc, token = await conv.create_session(S.store, chain, now=S.clock())
        return cors(JSONResponse({"session_id": doc["id"], "session_token": token, "expires_at": doc["expires_at"]},
                                 status_code=201), origin)

    @router.get("/public/instances/{pid}/sessions/{sid}/history")
    @translate
    async def chat_history(pid: str, sid: str, request: Request):
        await limit(request, "wc-history", LIMIT_HISTORY_IP)
        chain, eff = await context(pid)
        origin = origin_for(request, eff, unsafe=False)
        if await conv.verify_session(S.store, chain, sid, request.headers.get(SESSION_HEADER), now=S.clock()) is None:
            raise _nf()
        msgs = await conv.history(S.store, chain, sid)
        return cors(JSONResponse({"messages": [{"id": m.get("id"), "role": m["role"], "text": m["text"],
                                                "at": m.get("at"), "citations": m.get("citations") or []}
                                               for m in msgs]}), origin)

    # ── chat ──
    async def prepare(pid: str, body: ChatBody, request: Request):
        """Everything that can fail with an HTTP status happens before any AI work."""
        await limit(request, "wc-chat", LIMIT_CHAT_IP)
        chain, eff = await context(pid)
        origin = origin_for(request, eff, unsafe=True)
        text = (body.message or "").strip()
        if not text or len(text) > MAX_MESSAGE_CHARS:
            raise HTTPException(422, f"Messages must be 1-{MAX_MESSAGE_CHARS} characters.")
        cmid = body.client_message_id or str(uuid.uuid4())
        try:
            if uuid.UUID(cmid).version != 4:
                raise ValueError
        except (ValueError, AttributeError, TypeError):
            raise HTTPException(422, "client_message_id must be a UUIDv4.")
        new_token = None
        if body.session_id is None:
            await limit(request, "wc-session", LIMIT_SESSION_CREATE_IP)
            session, new_token = await conv.create_session(S.store, chain, now=S.clock())
        else:
            session = await conv.verify_session(S.store, chain, body.session_id, request.headers.get(SESSION_HEADER),
                                                now=S.clock())
            if session is None:
                raise _nf()                     # unknown, foreign, expired or wrong token: indistinguishable
        sid = session["id"]
        rid = conv.request_id(sid, cmid)
        status, record = await conv.begin_request(S.store, chain, sid, cmid, now=S.clock())
        if status == "busy":
            raise HTTPException(409, "This message is already being processed.")
        if status == "stale":
            # The previous attempt crashed. Re-run only if nothing was reserved
            # for this key; otherwise the provider outcome is uncertain and we
            # never generate (bill) again automatically.
            ledger = await Pipeline.usage_for(S).ledger_state(rid)
            if ledger in (None, "released", "rejected") and await conv.takeover(S.store, record, now=S.clock()):
                status = "new"
            else:
                await conv.mark_uncertain(S.store, rid)
                status, record = "uncertain", None
        if status == "new":
            try:
                await limit(request, "wc-per-session", LIMIT_PER_SESSION, ident=sid)
                if await conv.user_message_count(S.store, sid) >= MAX_USER_MESSAGES_PER_SESSION:
                    raise HTTPException(429, "This conversation is too long. Please start a new one.")
                await limit(request, "wc-daily", (S.settings.webchat_daily_limit_per_instance, 86400),
                            ident=chain.instance.id)
            except HTTPException:
                await conv.abandon_request(S.store, sid, cmid)          # nothing billed: retry allowed later
                raise
        return chain, eff, origin, sid, new_token, cmid, status, record, text

    async def complete(chain, eff, record: Dict[str, Any]) -> Dict[str, Any]:
        """Finish a request whose answer was generated and durably stored:
        conversation append, usage settlement and completion are all idempotent,
        so this is safe to repeat after any crash."""
        return await conv.complete_request(S.store, Pipeline.usage_for(S), chain, record, now=S.clock(),
                                           retention_days=eff.policy.get("retention.conversation_days"))

    async def outcome_of(chain, eff, status: str, record: Optional[dict], sid: str, cmid: str, text: str,
                         pid: str) -> Dict[str, Any]:
        if status == "done":
            return record["result"]
        if status == "uncertain":
            return UNCERTAIN
        if status == "generated":
            return await shielded(complete(chain, eff, record))
        return await shielded(run(chain, eff, sid, cmid, text, pid))

    async def run(chain, eff, sid: str, cmid: str, text: str, pid: str) -> Dict[str, Any]:
        """Run the pipeline once for a claimed request. Order: generate →
        durable result (request record) → usage settlement → conversation →
        done. Shielded by callers, so a client disconnect never loses a billed
        answer (a reconnect replays it)."""
        rid = conv.request_id(sid, cmid)
        reply_id = conv.reply_message_id(rid)
        try:
            hist = await conv.history(S.store, chain, sid, limit=20)
            pipeline = Pipeline(S.store, box=S.box, clock=S.clock, providers=S.providers, retriever=S.retriever,
                                catalog=S.catalog, platform_policy=S.platform_policy)

            def result_for(outcome) -> Dict[str, Any]:
                now = S.clock()
                return {"http_status": 200, "message_id": reply_id, "text": outcome.text,
                        "citations": list(outcome.citations), "billed": outcome.billed, "usage": dict(outcome.usage),
                        "messages": [{"id": cmid, "role": "user", "text": text, "at": now},
                                     {"id": reply_id, "role": "assistant", "text": outcome.text, "at": now,
                                      "citations": list(outcome.citations), "kind": outcome.kind}]}

            async def on_generated(outcome):
                await conv.mark_generated(S.store, rid, result_for(outcome))

            session_doc = await S.store.get(conv.SESSIONS, sid)
            labels = {"preview": True} if session_doc and session_doc.get("preview") is True else None
            outcome = await pipeline.handle(InboundMessage(
                ChannelAddress(ChannelType.WEBCHAT, public_id=pid), text, end_user_id=sid,
                history=tuple((m["role"], m["text"]) for m in hist if m.get("role") in ("user", "assistant"))),
                billing_key=rid, on_generated=on_generated, usage_labels=labels)
            if outcome.kind == "uncertain":
                await conv.mark_uncertain(S.store, rid)
                return UNCERTAIN
            if not outcome.deliver:
                result = {"http_status": 404, "message": NOT_FOUND}
            elif outcome.kind in ("rate_limited", "quota_exceeded"):
                result = {"http_status": 429, "message": outcome.text}
            elif outcome.kind == "busy" or outcome.admin_issue == "provider_error":
                await conv.abandon_request(S.store, sid, cmid)       # nothing billed (released): retry allowed
                return {"http_status": 503, "message": outcome.text or "Sorry, something went wrong. Please try again."}
            elif outcome.billed:
                record = await S.store.get(conv.REQUESTS, rid)    # generated result stored by on_generated
                return await complete(chain, eff, record)
            else:
                result = result_for(outcome)                     # refusal/fallback before any AI call: not billed
                await conv.append_turn(S.store, chain, sid, result["messages"], now=S.clock(),
                                       retention_days=eff.policy.get("retention.conversation_days"))
            await conv.finish_request(S.store, sid, cmid, result)
            return result
        except Exception:
            doc = await S.store.get(conv.REQUESTS, rid)
            if doc and doc.get("status") == "generated":
                return {"http_status": 503, "message": "Sorry, something went wrong. Please try again."}  # retry completes
            ledger = await Pipeline.usage_for(S).ledger_state(rid)
            if ledger in (None, "released", "rejected"):
                await conv.abandon_request(S.store, sid, cmid)       # nothing billed: safe to retry
                return {"http_status": 503, "message": "Sorry, something went wrong. Please try again."}
            await conv.mark_uncertain(S.store, rid)
            return UNCERTAIN

    def shielded(coro):
        return asyncio.shield(asyncio.ensure_future(coro))

    @router.post("/public/instances/{pid}/chat")
    @translate
    async def chat(pid: str, body: ChatBody, request: Request):
        chain, eff, origin, sid, new_token, cmid, status, record, text = await prepare(pid, body, request)
        result = await outcome_of(chain, eff, status, record, sid, cmid, text, pid)
        if result["http_status"] != 200:
            return cors(JSONResponse({"detail": result["message"]}, status_code=result["http_status"]), origin)
        out = {"session_id": sid, "message_id": result["message_id"], "reply": result["text"],
               "citations": result["citations"], "replayed": status in ("done", "generated")}
        if new_token:
            out["session_token"] = new_token
        return cors(JSONResponse(out), origin)

    @router.post("/public/instances/{pid}/chat/stream")
    @translate
    async def chat_stream(pid: str, body: ChatBody, request: Request):
        chain, eff, origin, sid, new_token, cmid, status, record, text = await prepare(pid, body, request)
        work = asyncio.ensure_future(outcome_of(chain, eff, status, record, sid, cmid, text, pid))

        async def events():
            first = {"session_id": sid}
            if new_token:
                first["session_token"] = new_token
            yield _sse("session", first)
            result = await asyncio.shield(work)
            if result["http_status"] != 200:
                yield _sse("error", {"message": result["message"]})
                return
            # Only validated output is ever sent: the whole answer passed the
            # knowledge gate and answer validation before the first token event.
            for piece in _chunks(result["text"]):
                yield _sse("token", {"token": piece})
            yield _sse("done", {"full_response": result["text"], "citations": result["citations"],
                                "message_id": result["message_id"]})
        resp = StreamingResponse(events(), media_type="text/event-stream")
        resp.headers["X-Accel-Buffering"] = "no"
        return cors(resp, origin)

    # ── widget, embed loader ──
    @router.get("/embed.js")
    async def embed_js():
        resp = Response(webchat_assets.embed_js(S.settings.public_api_origin), media_type="application/javascript")
        resp.headers["Cache-Control"] = "public, max-age=300"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    @router.get("/public/widget.js")
    async def widget_js():
        resp = Response(webchat_assets.WIDGET_JS, media_type="application/javascript")
        resp.headers["Cache-Control"] = "public, max-age=300"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    @router.get("/public/widget.css")
    async def widget_css():
        resp = Response(webchat_assets.WIDGET_CSS, media_type="text/css")
        resp.headers["Cache-Control"] = "public, max-age=300"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    @router.get("/public/widget/{pid}")
    @translate
    async def widget_page(pid: str, request: Request):
        await limit(request, "wc-read", LIMIT_PUBLIC_READ_IP)
        chain, eff = await context(pid)
        allowed = eff.values.get("channel.allowed_origins")
        ancestors = "*" if allowed is None else " ".join(["'self'"] + sorted(allowed))
        css_url = eff.public_values().get("widget.custom_css_url") or ""
        style_src = "'self'"
        if css_url:
            m = re.match(r"(https://[^/]+)", css_url)
            style_src += f" {m.group(1)}" if m else ""
        resp = HTMLResponse(webchat_assets.render_widget_html(pid, css_url))
        resp.headers["Content-Security-Policy"] = (
            f"default-src 'none'; script-src 'self'; style-src {style_src}; img-src 'self' data:; "
            f"connect-src 'self'; frame-ancestors {ancestors}; base-uri 'none'; form-action 'none'")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "strict-origin"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ── admin: conversations (W8), logo (W4), embed snippet (W9) ──
    async def instance_access(user: User, instance_id: str, action: Action):
        a = await load_access(S, user, Level.BOT_INSTANCE, instance_id, action)
        if a.target.channel.type is not ChannelType.WEBCHAT:
            raise HTTPException(404, NOT_FOUND)
        return a

    @router.get("/instances/{instance_id}/conversations")
    @translate
    async def list_conversations(instance_id: str, user: User = Depends(principal)):
        a = await instance_access(user, instance_id, Action.READ)
        docs = await S.store.find(conv.CONVERSATIONS, {"enterprise_id": a.chain.enterprise_id,
                                                       "instance_id": instance_id}, sort="updated_at",
                                  descending=True, limit=200)
        return {"conversations": [{"session_id": d["id"], "created_at": d.get("created_at"),
                                   "updated_at": d.get("updated_at"), "messages": len(d.get("messages") or [])}
                                  for d in docs]}

    @router.get("/instances/{instance_id}/conversations/{session_id}")
    @translate
    async def get_conversation(instance_id: str, session_id: str, user: User = Depends(principal)):
        a = await instance_access(user, instance_id, Action.READ)
        msgs = await conv.history(S.store, a.chain, session_id, limit=conv.MAX_STORED_MESSAGES)
        if not msgs:
            raise HTTPException(404, NOT_FOUND)
        return {"session_id": session_id, "messages": msgs}

    @router.post("/instances/{instance_id}/conversations/{session_id}/approve")
    @translate
    async def approve(instance_id: str, session_id: str, body: ApproveBody, user: User = Depends(principal)):
        """W8: approve a reply as a tone example for the Bot (ordered, capped at 10)."""
        a = await instance_access(user, instance_id, Action.EDIT_CONFIG)
        msgs = await conv.history(S.store, a.chain, session_id, limit=conv.MAX_STORED_MESSAGES)
        idx = next((i for i, m in enumerate(msgs) if m.get("id") == body.message_id and m["role"] == "assistant"), None)
        if idx is None or idx == 0 or msgs[idx - 1]["role"] != "user":
            raise HTTPException(404, NOT_FOUND)
        example = {"user": msgs[idx - 1]["text"][:1000], "bot": msgs[idx]["text"][:2000]}
        bot_chain = await S.hierarchy.load_chain(Level.BOT, a.chain.bot.id)
        current = list(resolver.resolve(bot_chain, platform_policy=S.platform_policy).values["behaviour.tone_examples"])
        current = [dict(e) for e in current if dict(e) != example] + [example]
        new = resolver.apply_override_patch(bot_chain, {"behaviour.tone_examples": current[-10:]},
                                            platform_policy=S.platform_policy)
        await S.hierarchy.set_overrides(Level.BOT, bot_chain.bot.id, new, expect_version=bot_chain.bot.version)
        await audit.record(S.store, actor=a.actor, action="conversation.approve", enterprise_id=a.chain.enterprise_id,
                           target_level="bot", target_id=bot_chain.bot.id, now=S.clock(),
                           details={"session_id": session_id})
        return {"tone_examples": len(current[-10:])}

    @router.put("/bots/{bot_id}/logo")
    @translate
    async def put_logo(bot_id: str, request: Request, user: User = Depends(principal)):
        a = await load_access(S, user, Level.BOT, bot_id, Action.EDIT_CONFIG)
        data = await request.body()
        content_type = sniff_logo(data)                 # magic bytes; ≤256 KB (D-Q8); client type ignored
        import hashlib
        meta = {"content_type": content_type, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        doc = {"id": bot_id, "enterprise_id": a.chain.enterprise_id, "organisation_id": a.chain.organisation.id,
               "bot_id": bot_id, "data": data, **meta, "version": 1}
        await S.store.delete(LOGOS, bot_id)
        await S.store.insert(LOGOS, doc)
        await S.hierarchy.set_bot_logo(bot_id, meta, expect_version=a.target.version)
        await audit.record(S.store, actor=a.actor, action="bot.logo", enterprise_id=a.chain.enterprise_id,
                           target_level="bot", target_id=bot_id, now=S.clock(), details=meta)
        return meta

    @router.delete("/bots/{bot_id}/logo")
    @translate
    async def delete_logo(bot_id: str, user: User = Depends(principal)):
        a = await load_access(S, user, Level.BOT, bot_id, Action.EDIT_CONFIG)
        await S.store.delete(LOGOS, bot_id)
        await S.hierarchy.set_bot_logo(bot_id, None, expect_version=a.target.version)
        await audit.record(S.store, actor=a.actor, action="bot.logo_removed", enterprise_id=a.chain.enterprise_id,
                           target_level="bot", target_id=bot_id, now=S.clock())
        return {"status": "removed"}

    @router.get("/instances/{instance_id}/embed-snippet")
    @translate
    async def embed_snippet(instance_id: str, user: User = Depends(principal)):
        a = await instance_access(user, instance_id, Action.READ)
        pid = a.target.channel.public_id
        base = S.settings.public_api_origin.rstrip("/")
        return {"public_id": pid,
                "script": f'<script src="{base}/api/embed.js" data-instance="{pid}" data-mode="popup" async></script>',
                "widget_url": f"{base}/api/public/widget/{pid}"}

    @router.post("/instances/{instance_id}/preview-session", status_code=201)
    @translate
    async def preview_session(instance_id: str, request: Request, user: User = Depends(principal)):
        """F5: an admin preview of the API-hosted widget. The session is marked
        ``preview`` server-side (a URL or body flag can never do this), so its
        usage is reported separately; it is still counted against every quota
        and billed like live traffic. Opened as a new tab on the API origin."""
        a = await instance_access(user, instance_id, Action.EDIT_CONFIG)    # cookie sessions: CSRF by the principal
        d = await throttle.hit(S.store, throttle.throttle_key("wc-preview", user.id), limit=PREVIEW_RATE[0],
                               window_seconds=PREVIEW_RATE[1], now=S.clock())
        if not d.allowed:
            raise HTTPException(429, "Too many preview sessions. Try again later.", headers={"Retry-After": str(d.retry_after)})
        if not a.chain.availability().available:
            raise HTTPException(409, "This instance is not available.")
        doc, token = await conv.create_session(S.store, a.chain, now=S.clock(), preview_by=user.id)
        await audit.record(S.store, actor=a.actor, action="webchat.preview_session", enterprise_id=a.chain.enterprise_id,
                           target_level="bot_instance", target_id=instance_id, now=S.clock(), details={"session_id": doc["id"]})
        base = S.settings.public_api_origin.rstrip("/")
        pid = a.target.channel.public_id
        # the token travels in the fragment (never sent to a server); the widget stores it and clears the fragment
        return {"preview_url": f"{base}/api/public/widget/{pid}?mode=fullpage#preview={doc['id']}.{token}",
                "expires_at": doc["expires_at"]}
