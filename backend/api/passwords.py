"""Password hashing and policy for the Spec 012 identity model (Phase 4A).

bcrypt (cost 12) keeps hashes compatible with the legacy users that the §15
reset retains (the platform superadmin). The password policy is the shared
``password_policy`` module (user passwords; the stricter superadmin rule for
bootstrap). Unknown accounts are checked against a dummy hash so timing does
not reveal whether an email exists.
"""
import bcrypt

import password_policy

ROUNDS = 12
_DUMMY = bcrypt.hashpw(b"dummy-password-for-timing-parity", bcrypt.gensalt(ROUNDS))


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(ROUNDS)).decode()


def verify_password(password: str, stored_hash: str) -> bool:
    if not isinstance(password, str) or not password or len(password.encode("utf-8")) > password_policy.MAX_LENGTH:
        bcrypt.checkpw(b"x", _DUMMY)
        return False
    if not stored_hash:
        bcrypt.checkpw(password.encode("utf-8"), _DUMMY)
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode())
    except ValueError:
        return False


def user_password_error(password: str, email: str):
    return password_policy.user_password_error(password, email)


def superadmin_password_error(password: str, email: str):
    problems = password_policy.check_password(password, email)
    return ("Password does not meet the requirements: " + "; ".join(problems) + ".") if problems else None
