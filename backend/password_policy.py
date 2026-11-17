"""Password policies (standard library only).

* ``check_password`` — strong policy for the superadmin bootstrap (spec 004, C6).
* ``check_user_password`` — policy for every user-chosen password: registration,
  password reset, password change, admin reset and invitation acceptance
  (spec 008, R-N6.1).

Standard library only. Reasons returned never include the password itself.
"""
from __future__ import annotations

import re
from typing import List

MIN_LENGTH = 16
LONG_PASSPHRASE_LENGTH = 24
MAX_LENGTH = 72  # bcrypt only uses the first 72 bytes

# Case-insensitive substrings that must not appear in the password.
DENYLIST = (
    "changeme", "password", "passw0rd", "admin", "administrator", "bitbotpro",
    "bridgebot", "botforge", "letmein", "welcome", "qwerty", "123456", "secret",
    "default",
)


def check_password(password: str, email: str) -> List[str]:
    """Return a list of policy violations; an empty list means acceptable."""
    problems: List[str] = []
    if not isinstance(password, str) or not password:
        return ["password is empty"]

    if password != password.strip():
        problems.append("password must not start or end with whitespace")
    if len(password) < MIN_LENGTH:
        problems.append(f"password must be at least {MIN_LENGTH} characters")
    if len(password.encode("utf-8")) > MAX_LENGTH:
        problems.append(f"password must be at most {MAX_LENGTH} bytes (bcrypt limit)")

    lowered = password.lower()
    for word in DENYLIST:
        if word in lowered:
            problems.append("password contains a common or product-related word")
            break

    local, _, domain = (email or "").lower().partition("@")
    tokens = set()
    if len(local) >= 3:
        tokens.add(local)
    for label in domain.split("."):
        if len(label) >= 4:
            tokens.add(label)
    if any(t in lowered for t in tokens):
        problems.append("password must not contain parts of the email address")

    classes = sum(
        bool(re.search(p, password))
        for p in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]")
    )
    if classes < 3 and len(password) < LONG_PASSPHRASE_LENGTH:
        problems.append(
            "password needs at least 3 of: lowercase, uppercase, digits, symbols "
            f"(or be at least {LONG_PASSPHRASE_LENGTH} characters)"
        )

    if len(set(password)) < 8:
        problems.append("password has too few distinct characters")

    return problems


# ── User passwords (spec 008, R-N6.1) ───────────────────────────────────────
USER_MIN_LENGTH = 12
USER_MIN_DISTINCT = 6


def check_user_password(password: str, email: str) -> List[str]:
    """Policy for user-chosen passwords. Returns violations (never the password)."""
    problems: List[str] = []
    if not isinstance(password, str) or not password:
        return ["password is empty"]
    if password != password.strip():
        problems.append("password must not start or end with whitespace")
    if len(password) < USER_MIN_LENGTH:
        problems.append(f"password must be at least {USER_MIN_LENGTH} characters")
    if len(password.encode("utf-8")) > MAX_LENGTH:
        problems.append(f"password must be at most {MAX_LENGTH} bytes")
    lowered = password.lower()
    if any(word in lowered for word in DENYLIST):
        problems.append("password contains a common or product-related word")
    local, _, domain = (email or "").lower().partition("@")
    tokens = {local} if len(local) >= 3 else set()
    tokens |= {label for label in domain.split(".") if len(label) >= 4}
    if any(t in lowered for t in tokens):
        problems.append("password must not contain parts of the email address")
    if len(set(password)) < USER_MIN_DISTINCT:
        problems.append("password has too few distinct characters")
    return problems


def user_password_error(password: str, email: str) -> str | None:
    """A single client-facing message, or None if the password is acceptable."""
    problems = check_user_password(password, email)
    if not problems:
        return None
    return "Password does not meet the requirements: " + "; ".join(problems) + "."
