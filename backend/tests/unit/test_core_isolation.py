"""Spec 012 Phase 2: the core package stays isolated from the running application."""
import ast
import unittest
from pathlib import Path

from tests.unit._support import BACKEND_DIR

CORE = Path(BACKEND_DIR) / "core"
FORBIDDEN_IN_CORE = {"server", "fastapi", "motor", "pymongo", "httpx", "aiohttp", "starlette",
                     "v2_helpers", "tenant_scope", "bot_routes", "org_routes", "workspace_routes",
                     "account_routes", "connection_routes", "webhook_router", "telegram_routes",
                     "zendesk_routes", "api_integrations_routes", "platform_routes", "migrate_v2",
                     "migrate_workspaces", "api", "persistence"}
ALLOWED_THIRD_PARTY = {"cryptography"}


def imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name.split(".")[0], node.lineno
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module.split(".")[0], node.lineno


class CoreIsolationTests(unittest.TestCase):
    def test_core_does_not_import_the_application_or_web_stack(self):
        offenders = []
        for f in sorted(CORE.glob("*.py")):
            for mod, line in imports(f):
                if mod in FORBIDDEN_IN_CORE:
                    offenders.append(f"core/{f.name}:{line} imports {mod}")
        self.assertEqual(offenders, [])

    def test_core_third_party_dependencies_are_limited(self):
        import sys
        stdlib = set(sys.stdlib_module_names) | {"core", "__future__"}
        offenders = [f"core/{f.name}:{line} {mod}" for f in sorted(CORE.glob("*.py"))
                     for mod, line in imports(f) if mod not in stdlib and mod not in ALLOWED_THIRD_PARTY]
        self.assertEqual(offenders, [])

    def test_application_does_not_import_core_yet(self):
        offenders = []
        for f in sorted(Path(BACKEND_DIR).glob("*.py")):
            for mod, line in imports(f):
                if mod == "core":
                    offenders.append(f"{f.name}:{line}")
        self.assertEqual(offenders, [], "Phase 2 modules must not be wired into the running app")

    def test_no_workspace_terminology_in_core(self):
        for f in sorted(CORE.glob("*.py")):
            self.assertNotIn("workspace", f.read_text(encoding="utf-8").lower(), f.name)


if __name__ == "__main__":
    unittest.main()
