"""Plain, escaped e-mail templates (Phase 6A, F3). Every interpolated value is
HTML-escaped and stripped of control characters; links are only the
server-built ones. No template ever contains a credential."""
from __future__ import annotations

import html
import re
from typing import Dict

_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _clean(value: str, limit: int = 200) -> str:
    return _CTRL.sub(" ", str(value or ""))[:limit].strip()


def invitation_email(*, enterprise_name: str, role: str, inviter_name: str, accept_url: str, expires_at: str) -> Dict[str, str]:
    ent, who, rl = _clean(enterprise_name), _clean(inviter_name) or "An administrator", _clean(role, 20)
    exp = _clean(expires_at, 40)
    subject = f"You have been invited to {ent} on BitBotPro"[:200]
    text = (f"{who} invited you to join {ent} on BitBotPro as {rl}.\n\n"
            f"Accept the invitation (sign in with this e-mail address first):\n{accept_url}\n\n"
            f"The link works once and expires on {exp}. If you did not expect this, ignore this e-mail.\n")
    e = html.escape
    body = (f"<p>{e(who)} invited you to join <strong>{e(ent)}</strong> on BitBotPro as {e(rl)}.</p>"
            f"<p><a href=\"{e(accept_url, quote=True)}\">Accept the invitation</a> "
            f"(sign in with this e-mail address first).</p>"
            f"<p>The link works once and expires on {e(exp)}. If you did not expect this, ignore this e-mail.</p>")
    return {"subject": subject, "text": text, "html": body}


__all__ = ["invitation_email"]
