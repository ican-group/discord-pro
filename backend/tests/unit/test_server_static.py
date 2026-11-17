"""Static (AST) regression checks on the backend source (spec 004).

These need no third-party packages, so they run anywhere. They guard the
properties that must not silently regress: no hard Emergent import, no startup
seeding/migrations, a single Discord choke point, strict CORS, and that every
MongoDB Knowledge Base access is behind the APP_MODE=api guard.
"""
import ast
import re
import unittest

from tests.unit._support import BACKEND_DIR

SERVER = BACKEND_DIR / "server.py"
SRC = SERVER.read_text(encoding="utf-8")
TREE = ast.parse(SRC)
APP_MODULES = sorted(p for p in BACKEND_DIR.glob("*.py") if p.name != "manage.py")


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def _route_path(fn):
    for dec in fn.decorator_list:
        if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                and dec.func.attr in {"get", "post", "put", "patch", "delete"}
                and dec.args and isinstance(dec.args[0], ast.Constant)):
            return dec.args[0].value
    return None


def _top_level_functions(tree):
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _names_in(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


class EmergentImportTests(unittest.TestCase):
    def test_no_module_level_emergentintegrations_import(self):
        for node in TREE.body:
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or "").startswith("emergentintegrations"))
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.startswith("emergentintegrations"))

    def test_emergent_import_is_inside_call_emergent(self):
        fn = _func(TREE, "_call_emergent")
        mods = [n.module for n in ast.walk(fn) if isinstance(n, ast.ImportFrom)]
        self.assertIn("emergentintegrations.llm.chat", mods)

    def test_requirements(self):
        req = (BACKEND_DIR / "requirements.txt").read_text()
        self.assertNotRegex(req, r"(?im)^emergentintegrations")
        self.assertRegex(req, r"(?m)^motor==3\.7\.1$")
        self.assertRegex(req, r"(?m)^pymongo==4\.10\.1$")


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.startup = _func(TREE, "startup_event")
        self.startup_src = ast.get_source_segment(SRC, self.startup)

    def test_no_admin_env_reads(self):
        self.assertNotIn('os.environ["ADMIN_EMAIL"]', SRC)
        self.assertNotIn('os.environ["ADMIN_PASSWORD"]', SRC)
        loads = {n.id for n in ast.walk(TREE) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        self.assertNotIn("ADMIN_EMAIL", loads)
        self.assertNotIn("ADMIN_PASSWORD", loads)

    def test_startup_does_not_create_or_modify_accounts_or_data(self):
        for forbidden in ("insert_one", "insert_many", "update_one", "update_many", "delete_one",
                          "delete_many", "replace_one", "bcrypt", "hashpw", "migrate",
                          "run_workspace_migration", "metadata.source"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.startup_src)

    def test_startup_kb_indexes_only_when_kb_enabled(self):
        for node in ast.walk(self.startup):
            if isinstance(node, ast.If) and "KB_DISABLED" in _names_in(node.test):
                body_src = "\n".join(ast.get_source_segment(SRC, s) for s in node.body)
                if "knowledge_sources" in body_src:
                    return
        self.fail("knowledge_sources index creation is not guarded by KB_DISABLED")

    def test_startup_discord_section_guarded(self):
        self.assertIn("if DISCORD_ALLOWED:", self.startup_src)


class DiscordTests(unittest.TestCase):
    def test_choke_point_guard_is_first_statement(self):
        fn = _func(TREE, "start_shared_discord_bot")
        body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
                and not isinstance(s, ast.Global)]
        first = body[0]
        self.assertIsInstance(first, ast.If)
        self.assertIn("DISCORD_ALLOWED", _names_in(first.test))
        self.assertIsInstance(first.body[-1], ast.Return)

    def test_all_callers_are_known_and_guarded(self):
        callers = set()
        for fn in _top_level_functions(TREE):
            for n in ast.walk(fn):
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                        and n.func.id == "start_shared_discord_bot"):
                    callers.add(fn.name)
                    self.assertIn("DISCORD_ALLOWED", _names_in(fn), fn.name)
        self.assertEqual(callers, {"startup_event", "discord_oauth_callback",
                                   "restart_discord_bot_endpoint"})

    def test_no_other_discord_client_construction(self):
        hits = [m.start() for m in re.finditer(r"discord\.Client\(", SRC)]
        self.assertEqual(len(hits), 1)
        for mod in APP_MODULES:
            if mod.name != "server.py":
                self.assertNotIn("discord.Client(", mod.read_text(encoding="utf-8"), mod.name)


class CorsTests(unittest.TestCase):
    def test_cors_uses_validated_origins(self):
        self.assertIn("allow_origins=CORS_ALLOWED_ORIGINS", SRC)
        self.assertNotIn("allow_origin_regex", SRC)
        self.assertNotRegex(SRC, r"CORS_ORIGINS['\"]\s*,\s*['\"]\*")

    def test_cors_validated_at_import(self):
        self.assertIn('_rc.parse_cors_origins(os.environ.get("CORS_ORIGINS"), APP_ENV)', SRC)


class HealthTests(unittest.TestCase):
    def test_health_route_pings_mongo(self):
        fn = _func(TREE, "health")
        self.assertEqual(_route_path(fn), "/health")
        self.assertIn('db.command("ping")', ast.get_source_segment(SRC, fn))


KB_COLLECTIONS = {"knowledge_sources", "knowledge_base"}
KB_CONTENT_METHODS = {
    "insert_one", "insert_many", "update_one", "update_many", "delete_one", "delete_many",
    "replace_one", "find", "find_one", "aggregate", "bulk_write", "distinct",
    "find_one_and_update", "find_one_and_delete", "find_one_and_replace", "create_index",
}


class KnowledgeBaseGuardTests(unittest.TestCase):
    """Every MongoDB KB content access must be behind the APP_MODE=api guard:
    a /knowledge route (503 middleware), search_knowledge (early return), or the
    KB_DISABLED-guarded startup index block. Counts (count_documents) are allowed."""

    def test_all_kb_routes_are_under_knowledge_prefix(self):
        for mod in APP_MODULES:
            tree = ast.parse(mod.read_text(encoding="utf-8"))
            for fn in ast.walk(tree):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    path = _route_path(fn)
                    if path and re.search(r"knowledge|/kb\b", path, re.I):
                        with self.subTest(module=mod.name, path=path):
                            self.assertEqual(mod.name, "server.py")
                            self.assertTrue(path == "/knowledge" or path.startswith("/knowledge/"))

    def test_kb_content_access_only_in_guarded_places(self):
        for mod in APP_MODULES:
            tree = ast.parse(mod.read_text(encoding="utf-8"))
            for fn in _top_level_functions(tree):
                path = _route_path(fn) or ""
                guarded = (mod.name == "server.py" and (
                    path.startswith("/knowledge") or fn.name in {"search_knowledge", "startup_event"}))
                for n in ast.walk(fn):
                    if (isinstance(n, ast.Attribute) and n.attr in KB_CONTENT_METHODS
                            and isinstance(n.value, ast.Attribute) and n.value.attr in KB_COLLECTIONS):
                        with self.subTest(module=mod.name, function=fn.name, method=n.attr):
                            self.assertTrue(guarded, f"unguarded KB access in {mod.name}:{fn.name}")
            for n in ast.walk(tree):
                if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant):
                    self.assertNotIn(n.slice.value, KB_COLLECTIONS, mod.name)

    def test_search_knowledge_returns_early_when_disabled(self):
        fn = _func(TREE, "search_knowledge")
        first = fn.body[0]
        self.assertIsInstance(first, ast.If)
        self.assertIn("KB_DISABLED", _names_in(first.test))
        self.assertIsInstance(first.body[-1], ast.Return)

    def test_kb_guard_middleware_registered_before_cors(self):
        guard = SRC.index("app.add_middleware(_KnowledgeBaseGuard)")
        cors = SRC.index("app.add_middleware(\n    CORSMiddleware")
        self.assertLess(guard, cors)  # added first => CORS is the outer layer



SECRET_NAMES = {"code", "invite_url", "token_val", "invite_token", "webhook_url", "webhook_secret",
                "reset_code", "verification_code", "hashed_pw", "hashed_password", "password",
                "new_password", "bot_token", "api_key", "client_secret", "access_token", "refresh_token"}


class CredentialLoggingTests(unittest.TestCase):
    """Phase 2b security fix: no credential is printed or logged by the API."""

    def test_no_print_in_api_modules(self):
        for mod in APP_MODULES:
            if mod.name == "email_delivery.py":
                continue  # contains only the hard-guarded DEVELOPMENT-ONLY console
            tree = ast.parse(mod.read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print":
                    self.fail(f"print() in {mod.name}:{n.lineno}")

    def test_no_secret_variables_interpolated_into_log_calls(self):
        for mod in APP_MODULES:
            tree = ast.parse(mod.read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr in {"debug", "info", "warning", "error", "exception", "critical"}
                        and isinstance(n.func.value, ast.Name) and n.func.value.id in {"logger", "log", "_log"}):
                    used = {x.id for a in n.args for x in ast.walk(a) if isinstance(x, ast.Name)}
                    with self.subTest(module=mod.name, line=n.lineno):
                        self.assertFalse(used & SECRET_NAMES, f"{mod.name}:{n.lineno} logs {used & SECRET_NAMES}")

    def test_email_flows_check_delivery_before_state_change(self):
        for name, write in (("register", "db.users.insert_one"),
                            ("resend_code", "db.users.update_one"),
                            ("forgot_password", "db.users.update_one"),
                            ("invite_user", "db.invitations.insert_one")):
            fn = _func(TREE, name)
            src = ast.get_source_segment(SRC, fn)
            with self.subTest(fn=name):
                self.assertIn("_require_email_delivery()", src)
                self.assertLess(src.index("_require_email_delivery()"), src.index(write))

    def test_failed_delivery_rolls_back(self):
        self.assertIn('db.users.delete_one({"id": user_id, "is_verified": False})',
                      ast.get_source_segment(SRC, _func(TREE, "register")))
        self.assertIn('db.invitations.delete_one({"token": token_val, "status": "pending"})',
                      ast.get_source_segment(SRC, _func(TREE, "invite_user")))
        forgot = ast.get_source_segment(SRC, _func(TREE, "forgot_password"))
        # Delivery runs in the background (spec 008 S1); if it finally fails, the
        # rollback removes exactly the code just issued (filter on its code id).
        self.assertIn('{"$unset": _auth.clear_code_fields(_auth.PURPOSE_RESET)}', forgot)
        self.assertIn("{\"id\": user_id, _auth.RESET_FIELDS.id: code_id}", forgot)
        self.assertIn("on_failure=_rollback", forgot)

    def test_email_error_maps_to_generic_503(self):
        fn = _func(TREE, "_email_delivery_error_handler")
        src = ast.get_source_segment(SRC, fn)
        self.assertIn("status_code=503", src)
        self.assertIn("EMAIL_UNAVAILABLE_DETAIL", src)

    def test_resend_key_is_optional(self):
        self.assertIn('resend.api_key = os.environ.get("RESEND_API_KEY", "")', SRC)
        self.assertNotIn('os.environ["RESEND_API_KEY"]', SRC)

    def test_log_redaction_installed(self):
        self.assertIn("_log_redaction.install()", SRC)
        self.assertIn("_log_redaction.install()", ast.get_source_segment(SRC, _func(TREE, "startup_event")))


class Stage2StaticTests(unittest.TestCase):
    """Spec 008 Stage 2 regression guards (static; no dependencies)."""

    ZENDESK = (BACKEND_DIR / "zendesk_routes.py").read_text(encoding="utf-8")
    TELEGRAM = (BACKEND_DIR / "telegram_routes.py").read_text(encoding="utf-8")

    def test_zendesk_suggestion_route_removed(self):
        for path in APP_MODULES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn in ast.walk(tree):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    route = _route_path(fn) or ""
                    with self.subTest(file=path.name, fn=fn.name):
                        self.assertFalse(route.startswith("/zendesk/suggestion"), route)
        self.assertNotIn("async def get_suggestion(", self.ZENDESK)

    def test_docs_disabled_in_production(self):
        self.assertRegex(SRC, r'"docs_url": None, "redoc_url": None, "openapi_url": None')
        self.assertIn("app = FastAPI(**_DOCS_KWARGS)", SRC)
        self.assertNotIn("app = FastAPI()", SRC)

    def test_login_and_me_scope_bots_to_user(self):
        for name in ("login", "get_me"):
            fn = _func(TREE, name)
            src = ast.get_source_segment(SRC, fn)
            with self.subTest(fn=name):
                self.assertIn("_tenant_scope.accessible_instances(db, user)", src)
                self.assertNotIn("db.bots.find(", src)

    def test_webhook_secrets_compared_in_constant_time(self):
        for name, src in (("zendesk_routes.py", self.ZENDESK), ("telegram_routes.py", self.TELEGRAM)):
            with self.subTest(file=name):
                self.assertNotRegex(src, r"webhook_secret\"\)\s*!=\s*webhook_secret")
                self.assertNotRegex(src, r"!=\s*webhook_secret")
                self.assertIn('_sec.secret_matches((cfg or {}).get("webhook_secret"), webhook_secret)', src)
                self.assertNotIn('"Forbidden"', src.split("_webhook(")[1].split("@router")[0])

    def test_sidebar_key_never_stored_or_looked_up_in_plaintext(self):
        tree = ast.parse(self.ZENDESK)
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key in node.keys:
                    if isinstance(key, ast.Constant) and key.value == "sidebar_api_key":
                        # Allowed only as a response field name, never in a DB filter/$set.
                        parent_src = ast.get_source_segment(self.ZENDESK, node) or ""
                        self.assertNotIn("$set", parent_src)
                        self.assertFalse(parent_src.lstrip("{").strip().startswith('"sidebar_api_key": key}') and
                                         "find_one" in parent_src)
        self.assertNotIn('find_one({"sidebar_api_key"', self.ZENDESK)
        self.assertNotIn('{"$set": {"sidebar_api_key"', self.ZENDESK)
        self.assertIn("_sec.sidebar_key_candidate_hash(raw)", self.ZENDESK)

    def test_request_values_not_logged_raw(self):
        """No logger call interpolates request headers directly (spec 008, L1)."""
        offenders = []
        for path in APP_MODULES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name) and node.func.value.id in ("logger", "log", "logging")
                        and node.func.attr in ("debug", "info", "warning", "error", "exception", "critical")):
                    for arg in node.args:
                        for sub in ast.walk(arg):
                            if isinstance(sub, ast.FormattedValue):
                                text = ast.unparse(sub.value)
                                if "request.headers" in text or "request.query_params" in text:
                                    offenders.append(f"{path.name}:{node.lineno}")
                        if not isinstance(arg, ast.JoinedStr):
                            text = ast.unparse(arg)
                            if ("request.headers" in text or "request.query_params" in text) \
                                    and "sanitize_for_log" not in text:
                                offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [])

    def test_startup_always_initialises_webhook_routes(self):
        """Webhook handlers depend on startup injecting their database handle;
        the calls must be unconditional statements of startup_event."""
        fn = _func(TREE, "startup_event")
        top_level_calls = {
            stmt.value.func.id for stmt in fn.body
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Name)
        }
        self.assertTrue({"_init_telegram", "_init_zendesk"} <= top_level_calls, top_level_calls)

    def test_discord_oauth_error_logging_sanitised(self):
        src = ast.get_source_segment(SRC, _func(TREE, "discord_oauth_callback"))
        self.assertIn("sanitize_for_log(error, 64)", src)
        self.assertIn("sanitize_for_log(error_description, 200)", src)
        self.assertIn("sanitize_for_log(guild.get('name', 'unknown'), 100)", src)
        self.assertNotIn('f"Discord OAuth error: {error}', src)


class Stage3StaticTests(unittest.TestCase):
    """Spec 008 Stage 3 regression guards (static)."""

    def test_auth_endpoints_rate_limited_per_ip(self):
        expected = {"register": "issue", "resend_code": "issue", "forgot_password": "issue",
                    "verify_email": "code", "reset_password": "code", "login": "login"}
        for fn_name, bucket in expected.items():
            src = ast.get_source_segment(SRC, _func(TREE, fn_name))
            with self.subTest(fn=fn_name):
                self.assertIn(f'_ip_guard(request, "{bucket}")', src)
                first_stmt = _func(TREE, fn_name).body[0]
                self.assertIn("_ip_guard", ast.unparse(first_stmt))

    def test_no_plaintext_codes_written(self):
        for pattern in (r'"verification_code": code', r'"reset_code": code', r"secrets\.randbelow\(900000\)"):
            self.assertNotRegex(SRC, pattern)

    def test_codes_checked_through_single_helper(self):
        for fn_name in ("verify_email", "reset_password"):
            src = ast.get_source_segment(SRC, _func(TREE, fn_name))
            with self.subTest(fn=fn_name):
                self.assertIn("await _check_code(", src)
                self.assertNotIn('user.get("verification_code") !=', src)
                self.assertNotIn('user.get("reset_code") !=', src)
                self.assertIn("modified_count != 1", src)        # atomic single use

    def test_every_jwt_decode_checks_token_version(self):
        sites = {"server.py": SRC, "v2_helpers.py": (BACKEND_DIR / "v2_helpers.py").read_text(encoding="utf-8")}
        for name, src in sites.items():
            tree = ast.parse(src)
            for fn in ast.walk(tree):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    body = ast.get_source_segment(src, fn) or ""
                    if ".decode(token" in body:
                        with self.subTest(file=name, fn=fn.name):
                            self.assertIn("token_version_matches(payload", body)

    def test_password_changes_bump_token_version(self):
        for fn_name in ("reset_password", "change_password", "admin_reset_password"):
            src = ast.get_source_segment(SRC, _func(TREE, fn_name))
            with self.subTest(fn=fn_name):
                self.assertIn('"$inc": {_auth.TOKEN_VERSION_FIELD: 1}', src)

    def test_user_password_policy_everywhere(self):
        for fn_name in ("register", "reset_password", "change_password", "admin_reset_password", "accept_invite"):
            src = ast.get_source_segment(SRC, _func(TREE, fn_name))
            with self.subTest(fn=fn_name):
                self.assertIn("_require_user_password(", src)
                self.assertNotRegex(src, r"len\((body\.)?(new_)?password\) < 6")

    def test_admin_reset_is_scoped(self):
        src = ast.get_source_segment(SRC, _func(TREE, "admin_reset_password"))
        self.assertIn("await _may_admin_reset_password(current_user, user)", src)
        self.assertLess(src.index("_may_admin_reset_password"), src.index("hashpw"))

    def test_code_key_fail_closed_at_import(self):
        self.assertIn("_rc.get_code_hmac_key(os.environ, APP_ENV, jwt_secret=JWT_SECRET)", SRC)


class Stage3bAuthorizationStaticTests(unittest.TestCase):
    """spec 008 N8: `require_admin_or_ws_admin` admits every verified user, so each
    endpoint using it must apply its own scoping."""

    SCOPING_MARKERS = ("_may_manage_user(", "_may_admin_reset_password(", "_validate_invitation_grants(",
                       "_invitation_scope_filter(", "_verify_instance_access(", "_api_key_in_scope(",
                       "is_super", "_tenant_scope.is_superadmin(")
    PLATFORM_ONLY = ("update_discord_app_config", "clear_discord_app_config", "list_discord_connections",
                     "purge_all_discord_connections")

    def _endpoints_using(self, dep):
        for fn in _top_level_functions(TREE):
            if any(isinstance(d, ast.Name) and d.id == dep for d in ast.walk(fn.args)):
                yield fn

    def test_every_ws_admin_endpoint_has_explicit_scoping(self):
        found = list(self._endpoints_using("require_admin_or_ws_admin"))
        self.assertGreaterEqual(len(found), 9)
        for fn in found:
            src = ast.get_source_segment(SRC, fn)
            with self.subTest(endpoint=fn.name):
                self.assertTrue(any(m in src for m in self.SCOPING_MARKERS), fn.name)

    def test_platform_discord_operations_require_superadmin(self):
        for name in self.PLATFORM_ONLY:
            fn = _func(TREE, name)
            deps = {d.id for d in ast.walk(fn.args) if isinstance(d, ast.Name)}
            with self.subTest(endpoint=name):
                self.assertIn("require_admin", deps)
                self.assertNotIn("require_admin_or_ws_admin", deps)

    def test_app_config_read_redacts_for_non_superadmins(self):
        src = ast.get_source_segment(SRC, _func(TREE, "get_discord_app_config"))
        self.assertIn("if not _tenant_scope.is_superadmin(current_user):", src)
        self.assertLess(src.index("is_superadmin"), src.index('"***"'))

    def test_invitations_validated_and_scoped(self):
        self.assertIn("await _validate_invitation_grants(current_user, body, workspace_role)",
                      ast.get_source_segment(SRC, _func(TREE, "invite_user")))
        for name in ("list_invitations", "revoke_invitation"):
            self.assertIn("await _invitation_scope_filter(current_user)",
                          ast.get_source_segment(SRC, _func(TREE, name)))
        self.assertIn('workspace_role: Optional[str] = "member"', SRC)

    def test_role_changes_cannot_insert_foreign_users(self):
        src = ast.get_source_segment(SRC, _func(TREE, "update_user_role"))
        self.assertIn("User is not a member of this workspace", src)
        self.assertIn("Only the workspace owner can manage admins", src)

    def test_delete_and_reset_share_one_rule(self):
        for name in ("delete_user", "admin_reset_password"):
            src = ast.get_source_segment(SRC, _func(TREE, name))
            with self.subTest(endpoint=name):
                self.assertTrue("await _may_manage_user(current_user, user)" in src
                                or "await _may_admin_reset_password(current_user, user)" in src)


class Stage4ChatStaticTests(unittest.TestCase):
    """spec 008 Stage 4 regression guards: public chat sessions."""

    def test_no_first_bot_fallback(self):
        src = ast.get_source_segment(SRC, _func(TREE, "_resolve_chat_instance"))
        self.assertNotIn("find_one({}", src)
        self.assertIn('raise not_found', src)

    def test_send_and_stream_run_preflight_first(self):
        for name in ("send_chat_message", "stream_chat_message"):
            fn = _func(TREE, name)
            with self.subTest(fn=name):
                stmts = [st for st in fn.body if not (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant))]
                self.assertIn("await _chat_preflight(request, body)", ast.unparse(stmts[0]))
                self.assertNotIn("body.session_id or", ast.get_source_segment(SRC, fn))

    def test_preflight_order_limits_before_work(self):
        src = ast.get_source_segment(SRC, _func(TREE, "_chat_preflight"))
        order = ['_ip_guard(request, "chat")', "MAX_CHAT_MESSAGE_CHARS", "_resolve_chat_instance",
                 "_CHAT_DAILY.record_failure", "_chat.resume_or_new"]
        positions = [src.index(x) for x in order]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("search_knowledge", src)
        self.assertNotIn("call_claude", src)

    def test_history_requires_token_header_and_scopes_query(self):
        src = ast.get_source_segment(SRC, _func(TREE, "get_chat_history"))
        self.assertIn('Header(None, alias="X-Chat-Session-Token")', src)
        self.assertIn("_chat.verify_token(_CHAT_SESSION_KEY, x_chat_session_token, session_id, instance_id)", src)
        self.assertIn('"platform": "web"', src)
        self.assertIn('"instance_id": instance_id', src)

    def test_save_conversation_scoped_and_atomic(self):
        src = ast.get_source_segment(SRC, _func(TREE, "save_conversation"))
        self.assertIn('{"session_id": session_id, "instance_id": instance_id}', src)
        self.assertIn("upsert=True", src)
        self.assertNotIn('find_one({"session_id": session_id})', src)

    def test_no_user_message_text_in_logs(self):
        self.assertNotRegex(SRC, r"logger\.\w+\(f?\".*\{user_message\[")
        self.assertNotRegex(SRC, r"logger\.\w+\(f?\".*reason', ''\)\[")

    def test_admin_conversation_search_is_literal(self):
        src = ast.get_source_segment(SRC, _func(TREE, "get_conversations"))
        self.assertIn("re.escape(", src)


class Stage9bStaticTests(unittest.TestCase):
    """spec 008 Stage 9b: sidebar app request path, embed script, public origins."""

    MAIN_JS = (BACKEND_DIR / "zendesk_sidebar_app" / "assets" / "main.js").read_text(encoding="utf-8")

    def test_sidebar_uses_zendesk_proxy_with_secure_setting(self):
        self.assertIn("client.request({", self.MAIN_JS)
        self.assertIn("secure: true", self.MAIN_JS)
        self.assertIn("cors: false", self.MAIN_JS)
        self.assertIn('"{{setting.sidebar_api_key}}"', self.MAIN_JS)
        self.assertNotIn("fetch(", self.MAIN_JS)
        self.assertNotIn("meta.settings.sidebar_api_key", self.MAIN_JS)   # secure settings are not readable

    def test_embed_script_never_splices_raw_query_values(self):
        src = ast.get_source_segment(SRC, _func(TREE, "serve_embed_script"))
        for raw in ('"{instance}"', '"{mode}"', '"{origin}"'):
            self.assertNotIn(raw, src)
        self.assertIn("{js(instance)}", src)
        # Every value interpolated into the script template goes through js(...):
        # comments included (a raw {instance} in a // comment was injectable).
        fn = _func(TREE, "serve_embed_script")
        templates = [n for n in ast.walk(fn) if isinstance(n, ast.JoinedStr)
                     and any(isinstance(v, ast.Constant) and "(function()" in str(v.value) for v in n.values)]
        self.assertEqual(len(templates), 1)
        for value in templates[0].values:
            if isinstance(value, ast.FormattedValue):
                expr = value.value
                with self.subTest(expr=ast.unparse(expr)):
                    self.assertTrue(isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name)
                                    and expr.func.id == "js", ast.unparse(expr))
        self.assertIn("request.base_url", src)          # development fallback only
        self.assertIn("_rc.public_url(_rc.PUBLIC_APP_URL) or fallback", src)

    def test_embed_config_returns_allowlisted_fields(self):
        src = ast.get_source_segment(SRC, _func(TREE, "get_embed_config"))
        self.assertIn('"Access-Control-Allow-Origin": "*"', src)
        self.assertNotIn("return config", src)

    def test_links_and_redirects_use_public_urls(self):
        self.assertIn("_rc.public_url(_rc.PUBLIC_APP_URL)", ast.get_source_segment(SRC, _func(TREE, "send_invitation_email")))
        self.assertIn("_rc.public_url(_rc.PUBLIC_APP_URL)", ast.get_source_segment(SRC, _func(TREE, "discord_oauth_callback")))


class Stages5to8StaticTests(unittest.TestCase):
    """spec 008 Stages 5-8 regression guards (static)."""

    def src(self, name):
        return ast.get_source_segment(SRC, _func(TREE, name))

    def test_register_is_invite_only_before_any_read(self):
        src = self.src("register")
        self.assertLess(src.index("REGISTRATION_MODE != _rc.REGISTRATION_OPEN"), src.index("db.users"))
        self.assertNotIn("Email already registered", src)

    def test_email_sends_run_in_background_with_uniform_responses(self):
        for name in ("resend_code", "forgot_password"):
            src = self.src(name)
            with self.subTest(fn=name):
                self.assertIn("background_tasks.add_task(_deliver_with_retry", src)
                self.assertNotIn("await send_", src)
        self.assertNotIn("Email already verified", self.src("verify_email"))

    def test_account_endpoints_have_response_floor(self):
        for name in ("register", "resend_code", "verify_email", "forgot_password", "reset_password"):
            fn = _func(TREE, name)
            with self.subTest(fn=name):
                self.assertIn("_with_response_floor", [ast.unparse(d) for d in fn.decorator_list])

    def test_discord_state_is_consumed_not_trusted(self):
        src = self.src("discord_oauth_callback")
        self.assertIn("await _oauth_state.consume(db[_oauth_state.COLLECTION], state, \"discord\")", src)
        self.assertNotIn('state or "default"', src)
        self.assertNotIn('"share:"', src)
        for name in ("get_discord_oauth_url", "get_discord_share_oauth_url"):
            self.assertIn("_oauth_state.issue(", self.src(name))
            self.assertNotIn('"state": instance_id', self.src(name))

    def test_invitation_acceptance_is_atomic_and_revalidated(self):
        src = self.src("accept_invite")
        self.assertIn('{"token": token, "status": "pending"}', src)
        self.assertIn("find_one_and_update", src)
        self.assertLess(src.index("_require_user_password"), src.index("find_one_and_update"))
        self.assertIn("_invitation_grants_at_acceptance(invite)", src)
        self.assertIn('"role": "user"', self.src("_create_invited_account"))

    def test_bot_info_has_no_persona(self):
        self.assertNotIn("persona", self.src("get_bot_info").split('"""')[-1])


class Stage10StaticTests(unittest.TestCase):
    def test_trusted_proxies_configured_at_import_and_fail_closed(self):
        self.assertIn("_client_ip.parse_trusted_proxies(os.environ.get(_client_ip.TRUSTED_PROXY_ENV))", SRC)
        self.assertIn("raise _rc.ConfigError(str(_e)) from None", SRC)

    def test_security_headers_middleware_is_outermost(self):
        adds = [n for n in TREE.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                and ast.unparse(n.value.func) == "app.add_middleware"]
        self.assertEqual(ast.unparse(adds[-1].value.args[0]), "_security_headers.SecurityHeadersMiddleware")
        self.assertIn("hsts=APP_ENV == _rc.APP_ENV_PRODUCTION", ast.unparse(adds[-1]))

    def test_no_code_reads_forwarded_headers_directly(self):
        for header in ("x-forwarded-for", "cf-connecting-ip", "x-real-ip"):
            self.assertNotIn(f'headers.get("{header}"', SRC.lower())


if __name__ == "__main__":
    unittest.main()
