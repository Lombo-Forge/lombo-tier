"""tiers.json: where every movie and season folder lives right now. A stable, versioned contract
other tools may read (docs/contracts.md). Written atomically after every run."""

import json
import os
from collections.abc import Iterable

from .core import Unit
from .pins import is_pinned

VERSION = 1


def build(units: Iterable[Unit], pinned: Iterable[str], roots: Iterable[str], now: float) -> dict[str, object]:
    pins, roots = list(pinned), tuple(roots)
    rows = [
        {
            "path": u.rel,
            # A title cut short midway has files on both; it counts as "ssd" until the next run finishes it.
            "tier": "ssd" if u.ssd_bytes > 0 else "hdd",
            "bytes": u.ssd_bytes + u.hdd_bytes,
            "pinned": is_pinned(u.rel, pins, roots),
        }
        for u in sorted(units, key=lambda u: u.rel)
    ]
    return {"version": VERSION, "updated": int(now), "units": rows}


def write(path: str, doc: dict[str, object]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=1)
    os.replace(tmp, path)
