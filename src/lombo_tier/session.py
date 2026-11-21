"""The conversion-session lock: a small file another tool keeps while it is converting media.

While it exists and has not expired, files must not move. Contract: `{"version": 1, "owner": "<tool>",
"expires": <unix seconds>}` (see docs/contracts.md). A file that cannot be read is NOT treated as closed:
the caller stands down, because the only safe guess about a lock nobody can read is "someone holds it".
"""

import json

OPEN = "open"
CLOSED = "closed"
UNREADABLE = "unreadable"


def check(path: str, now: float) -> tuple[str, str]:
    """(state, detail). No setting or no file is closed; an expired file is closed."""
    if not path:
        return CLOSED, ""
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return CLOSED, ""
    except (OSError, ValueError) as exc:
        return UNREADABLE, f"{type(exc).__name__}: {exc}"[:200]
    if not isinstance(data, dict) or data.get("version") != 1:
        return UNREADABLE, "not a version 1 session file"
    owner, expires = data.get("owner"), data.get("expires")
    if not isinstance(owner, str) or not owner.strip():
        return UNREADABLE, "the owner is empty"
    if isinstance(expires, bool) or not isinstance(expires, int | float):
        return UNREADABLE, "expires is not a number"
    if expires <= now:
        return CLOSED, f"expired (it was held by {owner.strip()[:60]})"
    return OPEN, f"held by {owner.strip()[:60]} until {int(expires)}"
