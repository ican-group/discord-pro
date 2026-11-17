"""Public-route inventory (spec 008, R-P7.5). Static (AST); runs everywhere.

Every route reachable without an authenticated session must be listed below with
a justification and the protections it must keep. Adding a new unauthenticated
route, or removing a listed protection, fails this test until it is reviewed.
"""
import ast
import re
import unittest

from tests.unit._support import BACKEND_DIR

PREFIX = {
    "server.py": "/api", "api_integrations_routes.py": "/api/integrations", "api_diagnostics_routes.py": "/api",
    "telegram_routes.py": "/api", "zendesk_routes.py": "/api", "workspace_routes.py": "/api/v2/workspaces",
    "org_routes.py": "/api/v2/orgs", "bot_routes.py": "/api/v2/orgs", "account_routes.py": "/api/v2/orgs",
    "connection_routes.py": "/api/v2/orgs", "platform_routes.py": "/api/v2/platform",
}
AUTH_DEPENDENCIES = {"get_current_user", "get_instance_access", "require_admin", "require_admin_or_ws_admin"}
BODY_AUTH = re.compile(r"await (?:h\.)?(?:_get_instance_access|_authed_user|require_org_member|_require_superadmin|"
                       r"require_platform_superadmin|_authenticate_sidebar|get_current_user|"
                       r"_get_instance_from_request|require_\w+)\(")

# (method, path): (justification, markers that must appear in the handler source)
PUBLIC_ROUTES = {
    ("GET", "/api/"): ("static banner", ()),
    ("GET", "/api/health"): ("readiness probe; reveals nothing", ()),
    ("GET", "/api/auth/registration-mode"): ("tells the frontend which form to show", ()),
    ("POST", "/api/auth/logout"): ("clears the caller's own cookie", ()),
    ("POST", "/api/auth/login"): ("authentication", ('_ip_guard(request, "login")', "_LOGIN_FAILURES.check")),
    ("POST", "/api/auth/register"): ("invite-only: uniform 403 in production",
                                     ('_ip_guard(request, "issue")', "REGISTRATION_MODE")),
    ("POST", "/api/auth/resend-code"): ("uniform response", ('_ip_guard(request, "issue")', "_may_issue_code")),
    ("POST", "/api/auth/forgot-password"): ("uniform response", ('_ip_guard(request, "issue")', "_may_issue_code")),
    ("POST", "/api/auth/verify"): ("code possession", ('_ip_guard(request, "code")', "_check_code")),
    ("POST", "/api/auth/reset-password"): ("code possession", ('_ip_guard(request, "code")', "_check_code")),
    ("GET", "/api/invite/{token}"): ("invitation token possession", ('_ip_guard(request, "invite")', "_usable_invitation")),
    ("POST", "/api/invite/{token}/accept"): ("invitation token possession; atomic claim",
                                             ('_ip_guard(request, "invite")', "find_one_and_update")),
    ("POST", "/api/chat/send"): ("public widget chat", ("_chat_preflight",)),
    ("POST", "/api/chat/stream"): ("public widget chat", ("_chat_preflight",)),
    ("GET", "/api/chat/history/{session_id}"): ("session-token possession",
                                                ('_ip_guard(request, "chat_history")', "_chat.verify_token")),
    ("GET", "/api/bot-info/{bot_id}"): ("widget header (name only)", ('_ip_guard(request, "public_read")',)),
    ("GET", "/api/bot-logo/{instance_id}"): ("widget logo image", ('_ip_guard(request, "public_read")',)),
    ("GET", "/api/embed/config/{instance_id}"): ("widget theming, allowlisted fields",
                                                  ('_ip_guard(request, "public_read")', '"Access-Control-Allow-Origin": "*"')),
    ("GET", "/api/embed.js"): ("embed loader script; values JSON-escaped", ("js(instance)",)),
    ("GET", "/api/discord/callback"): ("OAuth redirect from Discord", ("_oauth_state.consume",)),
    ("GET", "/api/zendesk/oauth/callback"): ("OAuth redirect from Zendesk", ("find_one_and_delete",)),
    ("POST", "/api/telegram/webhook/{instance_id}/{webhook_secret}"): (
        "Telegram webhook", ("check_webhook_rate", "secret_matches", "telegram_secret_valid", "read_body_limited")),
    ("POST", "/api/zendesk/webhook/{instance_id}/{webhook_secret}"): (
        "Zendesk webhook", ("check_webhook_rate", "secret_matches", "zendesk_signature_valid", "read_body_limited")),
}


def discover_public_routes():
    found = {}
    for name, prefix in PREFIX.items():
        src = (BACKEND_DIR / name).read_text(encoding="utf-8")
        for fn in ast.walk(ast.parse(src)):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in fn.decorator_list:
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr in {"get", "post", "put", "patch", "delete"}
                        and dec.args and isinstance(dec.args[0], ast.Constant)):
                    path = ("" if ast.unparse(dec.func.value) == "app" else prefix) + dec.args[0].value
                    deps = {n.id for n in ast.walk(fn.args) if isinstance(n, ast.Name)}
                    body = ast.get_source_segment(src, fn) or ""
                    if not (deps & AUTH_DEPENDENCIES) and not BODY_AUTH.search(body):
                        found[(dec.func.attr.upper(), path)] = body
    return found


class PublicRouteInventoryTests(unittest.TestCase):
    def test_public_routes_match_the_reviewed_inventory(self):
        found = discover_public_routes()
        self.assertEqual(sorted(found), sorted(PUBLIC_ROUTES),
                         "a route was added or lost its authentication: review it and update PUBLIC_ROUTES")
        for route, body in found.items():
            _why, markers = PUBLIC_ROUTES[route]
            for marker in markers:
                with self.subTest(route=route, marker=marker):
                    self.assertIn(marker, body)


if __name__ == "__main__":
    unittest.main()
