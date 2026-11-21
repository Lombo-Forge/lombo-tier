"""Pinned titles: a short list of titles the nightly run and `move` never take off the SSD.

One JSON file, `{"version": 1, "titles": ["movies/Jaws (1975)", ...]}`. An entry is a title folder name
or a path inside the library. A file that exists but cannot be read or parsed is a problem the caller
must stand down for: reading it as an empty list would let a run demote every pinned title.
"""

import json
import os
from collections.abc import Iterable

VERSION = 1


def _parse(path: str) -> tuple[list[str], str | None]:
    """(titles, problem). A missing file is an empty list with no problem."""
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return [], None
    except (OSError, ValueError) as exc:
        return [], f"{type(exc).__name__}: {exc}"[:200]
    if not isinstance(data, dict):
        return [], "not a JSON object"
    if data.get("version") != VERSION:
        return [], f"unknown version {data.get('version')!r} (this release reads version {VERSION})"
    titles = data.get("titles")
    if not isinstance(titles, list) or not all(isinstance(t, str) and t.strip() for t in titles):
        return [], '"titles" must be a list of title names'
    clean = [t.strip().strip("/") for t in titles]
    if any(not t or ".." in t.split("/") for t in clean):
        return [], 'a title in "titles" is empty or contains ".."'
    return clean, None


def problem(path: str) -> str | None:
    """Why the pin list can't be trusted, or None (also None when no file is set or it does not exist yet)."""
    return _parse(path)[1] if path else None


def load(path: str) -> list[str]:
    """The pinned titles. Empty for no file. Raises ValueError for a damaged one: never read it as empty."""
    if not path:
        return []
    titles, bad = _parse(path)
    if bad:
        raise ValueError(f"the pin list {path} can't be read ({bad})")
    return titles


def save(path: str, titles: Iterable[str]) -> None:
    """Write the list atomically: a crash leaves the old file or the new one, never half of one."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"version": VERSION, "titles": sorted(set(titles))}, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def is_pinned(rel: str, titles: Iterable[str], roots: Iterable[str]) -> bool:
    """Is the unit or title at pool-relative `rel` covered by an entry? An entry covers that folder and
    everything under it, so pinning a show covers all its seasons."""
    for t in titles:
        for cand in (t, *(f"{r}/{t}" for r in roots)):
            if rel == cand or rel.startswith(cand + "/"):
                return True
    return False
