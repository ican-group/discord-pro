"""Container health check for both roles of the Spec 012 image (Phase 6B).

APP_MODE=api    → GET http://127.0.0.1:8000/api/health must answer 200 (no database detail)
APP_MODE=worker → the worker heartbeat file must be fresh (written every 10 s)

No network access beyond the loopback, no secrets read, nothing printed.
"""
import os
import sys
import urllib.request


def main() -> int:
    mode = (os.environ.get("APP_MODE") or "").strip().lower()
    if mode == "worker":
        from api.production import worker_healthy
        return 0 if worker_healthy() else 1
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=3) as r:
            return 0 if r.status == 200 else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
