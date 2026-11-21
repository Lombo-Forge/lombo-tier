"""Turn Jellyfin's item and session data into the planner's Watch objects."""

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from . import core
from .core import Watch
from .jellyfin import Jellyfin


def parse_ts(value: object) -> float | None:
    """Jellyfin's ISO date text to a Unix time, or None if it is empty or unreadable."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()  # accepts "Z" and 7-digit fractions
    except ValueError:
        return None


def build_watch(
    created_items: Iterable[dict[str, Any]],
    per_user_items: Iterable[Iterable[dict[str, Any]]],
    user_ids: Sequence[str],
    prefix: str,
) -> dict[str, Watch]:
    """Merge the item list and every user's played items into one Watch per file.

    `user_ids` names who each list belongs to (a person can have several lists),
    so popularity counts people, not lists. Files outside `prefix` are skipped."""
    watch: dict[str, Watch] = {}
    counted: dict[tuple[str, str], int] = {}

    def entry(item: dict[str, Any]) -> Watch | None:
        if not item.get("Path"):
            return None
        rel = core.rel_from_media_path(item["Path"], prefix)
        return None if rel is None else watch.setdefault(rel, Watch())

    for it in created_items:
        w = entry(it)
        if w is not None:
            w.created = parse_ts(it.get("DateCreated"))
    for i, items in enumerate(per_user_items):
        uid = user_ids[i] if user_ids else str(i)
        for it in items:
            w = entry(it)
            if w is None:
                continue
            ud = it.get("UserData") or {}
            last = parse_ts(ud.get("LastPlayedDate"))
            if last is not None:
                w.last_played = max(w.last_played or 0, last)
                w.viewers[uid] = max(w.viewers.get(uid, 0), last)
            # The same item can be in a person's played list and again in their resumable list: count it once.
            count = int(ud.get("PlayCount") or 0)
            key = (uid, it["Path"])
            w.plays += max(0, count - counted.get(key, 0))
            counted[key] = max(count, counted.get(key, 0))
    return watch


def playing_paths(sessions: Iterable[dict[str, Any]] | None, prefix: str) -> set[str]:
    """Pool-relative paths of everything being played right now."""
    out: set[str] = set()
    for s in sessions or []:
        path = (s.get("NowPlayingItem") or {}).get("Path")
        rel = core.rel_from_media_path(path, prefix) if path else None
        if rel is not None:
            out.add(rel)
    return out


def collect_watch(jf: Jellyfin, prefix: str) -> dict[str, Watch]:
    created = jf.paged(
        "/Items",
        Recursive="true",
        IncludeItemTypes="Movie,Episode",
        Fields="Path,DateCreated",
        CollapseBoxSetItems="false",
    )
    per_user, ids = [], []
    for u in jf.users():
        for flt in ("IsPlayed", "IsResumable"):
            per_user.append(
                jf.paged(
                    f"/Users/{u['Id']}/Items",
                    Recursive="true",
                    IncludeItemTypes="Movie,Episode",
                    Fields="Path",
                    Filters=flt,
                    EnableUserData="true",
                    CollapseBoxSetItems="false",
                )
            )
            ids.append(u["Id"])
    return build_watch(created, per_user, ids, prefix)


def current_playing(jf: Jellyfin, prefix: str) -> set[str]:
    return playing_paths(jf.sessions() or [], prefix)
