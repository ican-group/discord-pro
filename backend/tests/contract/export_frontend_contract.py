"""Export the Spec 012 API contract for the Phase 5 frontend.

Writes (into ``<out>``):
- ``routes.json``: every mounted ``/api`` route as ``"METHOD /path"`` (the
  frontend test fails if the client calls a route that is not listed, so no
  route is invented);
- ``samples.json``: real responses of representative GET routes from the
  in-memory test World (synthetic data, no secrets), used as frontend mocks.

Run in the test image:  python -m tests.contract.export_frontend_contract <out>
"""
import asyncio
import json
import re
import sys


def route_manifest(app):
    out = set()
    from api.app import route_table
    for r in route_table(app):
        path = getattr(r, "path", "")
        if not path.startswith("/api/"):
            continue
        path = re.sub(r"\{([a-z_]+):[a-z_]+\}", r"{\1}", path)[4:]
        for m in sorted(getattr(r, "methods", ()) or ()):
            if m in ("HEAD", "OPTIONS"):
                continue
            out.add(f"{m} {path}")
    return sorted(out)


async def samples():
    from tests.unit._api_fixtures import World
    w = await World().build()
    S = w.S
    # a Zendesk integration, a knowledge source and a pending invitation for realistic shapes
    zi = (await w.call("POST", f"/bots/{w.bA.id}/integrations", "admin",
                       {"type": "zendesk", "name": "Support desk", "settings": {"reply_mode": "agent_assist"}})).json()
    src = (await w.call("POST", f"/enterprises/{w.eA.id}/knowledge/sources", "owner",
                        {"name": "Policies"})).json()
    await w.call("POST", f"/enterprises/{w.eA.id}/invitations", "owner",
                 {"email": "new.person@example.test", "role": "viewer"})
    gets = {
        "session": ("/auth/session", "owner"),
        "session_root": ("/auth/session", "root"),
        "enterprises": ("/enterprises", "owner"),
        "enterprise": (f"/enterprises/{w.eA.id}", "owner"),
        "organisations": (f"/enterprises/{w.eA.id}/organisations", "owner"),
        "organisation": (f"/organisations/{w.oA.id}", "owner"),
        "bots": (f"/organisations/{w.oA.id}/bots", "owner"),
        "bot": (f"/bots/{w.bA.id}", "owner"),
        "instances": (f"/bots/{w.bA.id}/instances", "owner"),
        "instance_web": (f"/instances/{w.iA_web.id}", "owner"),
        "instance_discord": (f"/instances/{w.iA_dc.id}", "owner"),
        "instance_telegram": (f"/instances/{w.iA_tg.id}", "owner"),
        "config_bot": (f"/bots/{w.bA.id}/config", "owner"),
        "policy_enterprise": (f"/enterprises/{w.eA.id}/policy", "owner"),
        "members_enterprise": (f"/enterprises/{w.eA.id}/members", "owner"),
        "invitations": (f"/enterprises/{w.eA.id}/invitations", "owner"),
        "credentials_enterprise": (f"/enterprises/{w.eA.id}/credentials", "owner"),
        "integrations": (f"/bots/{w.bA.id}/integrations", "owner"),
        "integration": (f"/integrations/{zi.get('id')}", "owner"),
        "integration_config": (f"/integrations/{zi.get('id')}/config", "owner"),
        "knowledge_sources": (f"/enterprises/{w.eA.id}/knowledge/sources", "owner"),
        "knowledge_documents": (f"/knowledge/sources/{src.get('id')}/documents", "owner"),
        "discord_status": (f"/instances/{w.iA_dc.id}/channel/status", "owner"),
        "discord_enterprise": (f"/enterprises/{w.eA.id}/discord", "owner"),
        "discord_app_platform": (f"/platform/enterprises/{w.eA.id}/discord-application", "root"),
        "platform_credentials": ("/platform/credentials", "root"),
        "platform_workers": ("/platform/workers", "root"),
        "audit_enterprise": (f"/enterprises/{w.eA.id}/audit", "owner"),
        "conversations": (f"/instances/{w.iA_web.id}/conversations", "owner"),
        "embed_snippet": (f"/instances/{w.iA_web.id}/embed-snippet", "owner"),
        "deliveries_tg": (f"/instances/{w.iA_tg.id}/deliveries", "owner"),
    }
    out = {"_ids": {"enterprise": w.eA.id, "organisation": w.oA.id, "bot": w.bA.id, "instance_web": w.iA_web.id,
                    "instance_discord": w.iA_dc.id, "instance_telegram": w.iA_tg.id, "integration": zi.get("id"),
                    "source": src.get("id")}}
    for name, (path, who) in gets.items():
        r = await w.call("GET", path, who)
        out[name] = {"path": path, "status": r.status_code, "body": r.json()}
    w.discord.guild_channels = getattr(w.discord, "guild_channels", {})
    more = {
        "discord_channels": (f"/instances/{w.iA_dc.id}/channel/discord/channels", "owner"),
        "discord_share_links": (f"/instances/{w.iA_dc.id}/channel/discord/share-links", "owner"),
        "discord_handoffs": (f"/instances/{w.iA_dc.id}/channel/discord/handoffs", "owner"),
        "zendesk_events": (f"/integrations/{zi.get('id')}/events", "owner"),
        "zendesk_suggestions": (f"/integrations/{zi.get('id')}/suggestions", "owner"),
        "zendesk_deliveries": (f"/integrations/{zi.get('id')}/deliveries", "owner"),
        "zendesk_webhook": (f"/integrations/{zi.get('id')}/zendesk/webhook", "owner"),
        "telegram_status": (f"/instances/{w.iA_tg.id}/channel/status", "owner"),
        "webchat_status": (f"/instances/{w.iA_web.id}/channel/status", "owner"),
        "runtime_config": (f"/instances/{w.iA_web.id}/runtime-config", "owner"),
        "platform_audit": ("/platform/audit", "root"),
        "users_me": ("/users/me", "owner"),
        "usage_org": (f"/organisations/{w.oA.id}/usage", "owner"),
        "usage_integration": (f"/integrations/{zi.get('id')}/usage", "owner"),
        "platform_users": ("/platform/users?limit=3", "root"),
        "member_candidates": (f"/organisations/{w.oA.id}/member-candidates?q=mem", "owner"),
    }
    for name, (path, who) in more.items():
        r = await w.call("GET", path, who)
        out[name] = {"path": path, "status": r.status_code, "body": r.json()}
    hook = out["zendesk_webhook"]["body"]
    hook["url"] = hook["url"].rsplit("/", 1)[0] + "/<path-secret>"      # synthetic, but never a usable secret
    r = await w.call("POST", f"/instances/{w.iA_dc.id}/channel/discord/share-links", "owner", {})
    out["discord_share_create"] = {"status": r.status_code, "body": r.json()}
    r = await w.call("POST", f"/instances/{w.iA_dc.id}/channel/discord/install-url", "owner")
    out["discord_install_url"] = {"status": r.status_code, "body": r.json()}
    r = await w.call("POST", "/invitations/lookup", None, {"token": "x" * 43})
    out["invitation_lookup_bad"] = {"status": r.status_code, "body": r.json()}
    # 6C B-ACCT: account creation from an e-mailed invitation (real response shapes)
    class _Capture:
        sent = []

        async def send_invitation(self, email, *, subject, text, html):
            self.sent.append(text)
    S.mailer = _Capture()
    await w.call("POST", f"/enterprises/{w.eA.id}/invitations", "owner",
                 {"email": "registering.person@example.test", "role": "editor"})
    reg_token = _Capture.sent[-1].split("#token=")[1].split()[0]
    hdr = {"X-Requested-With": "BitBotPro"}
    r = await w.call("POST", "/invitations/register", None,
                     {"token": reg_token, "name": "New Person", "password": "short"}, headers=hdr)
    out["invitation_register_weak"] = {"status": r.status_code, "body": r.json()}
    r = await w.call("POST", "/invitations/register", None,
                     {"token": reg_token, "name": "New Person", "password": "Orchid-Lantern-Harbour-2026"}, headers=hdr)
    out["invitation_register"] = {"status": r.status_code, "body": r.json()}
    r = await w.call("POST", "/invitations/register", None,
                     {"token": reg_token, "name": "New Person", "password": "Orchid-Lantern-Harbour-2026"}, headers=hdr)
    out["invitation_register_used"] = {"status": r.status_code, "body": r.json()}
    r = await w.call("POST", f"/integrations/{zi.get('id')}/sidebar-key", "owner")
    out["sidebar_key_create"] = {"status": r.status_code, "body": {k: ("bbz_" + "x" * 43 if k == "sidebar_key" else v)
                                                                   for k, v in r.json().items()}}
    prev = await w.call("POST", f"/organisations/{w.oA.id}/deletion-preview", "owner")
    out["deletion_preview"] = {"status": prev.status_code, "body": prev.json()}
    pv = prev.json()
    r = await w.call("POST", f"/organisations/{w.oA.id}/delete", "owner",
                     {"preview_token": pv.get("preview_token") or pv.get("token") or "", "confirm_name": pv.get("confirm_name")})
    out["deletion_start"] = {"status": r.status_code, "body": r.json()}
    jid = (r.json() or {}).get("id")
    if jid:
        r = await w.call("GET", f"/deletion-jobs/{jid}", "owner")
        out["deletion_job"] = {"status": r.status_code, "body": r.json()}
    # every status an OAuth callback can report (the result page must explain each one)
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2] / "api"
    found = set()
    for name in ("discord.py", "integrations.py"):
        found |= set(re.findall(r'page\(\d+, "([a-z_]+)"', (root / name).read_text()))
    out["_oauth_statuses"] = sorted(found)
    return out, route_manifest(w.app)


def main(target):
    data, routes = asyncio.run(samples())
    with open(f"{target}/routes.json", "w") as fh:
        json.dump(routes, fh, indent=1)
        fh.write("\n")
    with open(f"{target}/samples.json", "w") as fh:
        json.dump(data, fh, indent=1, sort_keys=True, default=str)
        fh.write("\n")
    print(f"{len(routes)} routes, {len(data)} samples")


if __name__ == "__main__":
    main(sys.argv[1])
