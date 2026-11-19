"""Spec 012 post-cutover smoke test against the PUBLIC API (operator-run, interactive).

    docker run --rm -it -v "$PWD/deploy/scripts/spec012-smoke.py:/smoke.py:ro" \
        --entrypoint python bitbotpro-api:spec012-beta1-4219354d1469 /smoke.py

Prompts for the superadmin password (never echoed, printed or stored), then checks login, the
session cookie flags, CSRF, Enterprise/Organisation/Bot creation (in a validation Enterprise that
is kept for the real-service tests), invitation/auth routes and logout. Prints results only:
never the password, the session cookie or any token.
"""
import getpass
import sys
import time

import httpx

API = "https://api.bitbotpro.io"
ORIGIN = "https://bitbotpro.io"
EMAIL = "admin@bitbotpro.io"
CSRF = {"X-Requested-With": "BitBotPro", "Origin": ORIGIN}
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")


def main() -> int:
    pw = getpass.getpass(f"Password for {EMAIL}: ")
    with httpx.Client(base_url=API, timeout=20, follow_redirects=False) as c:
        check("public ready", c.get("/api/ready").json().get("status") == "ready")
        r = c.post("/api/auth/login", json={"email": EMAIL, "password": pw}, headers={"Origin": ORIGIN})
        check("login without the CSRF header is refused", r.status_code == 403, str(r.status_code))
        r = c.post("/api/auth/login", json={"email": EMAIL, "password": pw}, headers=CSRF)
        del pw
        check("superadmin login", r.status_code == 200, str(r.status_code))
        if r.status_code != 200:
            return 1
        cookie = r.headers.get("set-cookie", "").lower()
        for flag in ("httponly", "secure", "samesite=strict", "path=/api"):
            check(f"session cookie {flag}", flag in cookie)
        check("CORS allows the admin origin with credentials",
              r.headers.get("access-control-allow-origin") == ORIGIN
              and r.headers.get("access-control-allow-credentials") == "true")
        s = c.get("/api/auth/session").json()
        check("session is the platform superadmin", bool(s.get("user", {}).get("is_platform_superadmin")))
        stamp = time.strftime("%Y%m%d%H%M%S")
        body = {"name": "BitBotPro Validation", "slug": f"bitbotpro-validation-{stamp}"}
        check("cookie session without the CSRF header cannot write",
              c.post("/api/enterprises", json=body, headers={"Origin": ORIGIN}).status_code == 403)
        r = c.post("/api/enterprises", json=body, headers=CSRF)
        check("Enterprise creation", r.status_code in (200, 201), str(r.status_code))
        eid = r.json().get("id") if r.status_code in (200, 201) else None
        r = c.post(f"/api/enterprises/{eid}/organisations", json={"name": "QA"}, headers=CSRF)
        check("Organisation creation", r.status_code in (200, 201), str(r.status_code))
        oid = r.json().get("id") if r.status_code in (200, 201) else None
        r = c.post(f"/api/organisations/{oid}/bots", json={"name": "QA Bot"}, headers=CSRF)
        check("Bot creation", r.status_code in (200, 201), str(r.status_code))
        bot = r.json() if r.status_code in (200, 201) else {}
        check("Bot ancestry is server-assigned",
              bot.get("enterprise_id") == eid and bot.get("organisation_id") == oid)
        check("Enterprise list shows it", any(e.get("id") == eid for e in c.get("/api/enterprises").json().get("enterprises", [])))
        check("invitation lookup with a bad token is 404",
              c.post("/api/invitations/lookup", json={"token": "x" * 43}, headers=CSRF).status_code == 404)
        check("forgot-password answers 202 (uniform)",
              c.post("/api/auth/forgot-password", json={"email": "nobody@example.invalid"}, headers=CSRF).status_code == 202)
        check("logout", c.post("/api/auth/logout", headers=CSRF).status_code == 200)
        check("session gone after logout", c.get("/api/auth/session").status_code == 401)
        print(f"\nValidation Enterprise kept for real-service tests: 'BitBotPro Validation' ({(eid or '')[:8]}…)")
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
