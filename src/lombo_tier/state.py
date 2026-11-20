"""The state file: the last run's result and a cooldown per title, so a title that just moved
does not bounce straight back. One JSON object in DATA_DIR/state.json.

A state file that exists but cannot be read must stop a run: carrying on would overwrite it and
forget every cooldown, and titles could start bouncing between disks.
"""

import json
import os
from typing import Any


def load(path: str) -> dict[str, Any]:
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def problem(path: str) -> str | None:
    """Why the file can't be trusted (it exists but can't be read or parsed), or None."""
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"[:200]
    return None if isinstance(data, dict) else "not a JSON object"


def save(path: str, state: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, path)
