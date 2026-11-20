"""The decisions: what is hot, what is cold, and what to move tonight. Pure.

No network, no disk, no clock (the caller passes `now`), so every rule here is tested with plain data.

The unit that moves is a movie folder or one season folder, so a title never ends up half on each
disk because of this tool. Paths in the pool never change (mergerfs shows the same tree), so Jellyfin,
Sonarr and Radarr never notice a move.

- DEMOTE (SSD -> HDD) only when the SSD is nearly full: the least popular, longest-unwatched titles
  first. Never a title that is playing, still hard-linked to a seeding torrent, recently promoted,
  or newer than `min_age_days`. Stops when the SSD has reserve + headroom free.
- PROMOTE (HDD -> SSD) titles that are being watched: several episodes of a series this week (the
  season in progress and the next one come over), a movie played often this month, or anything
  several different people watched this month. Only if the SSD can take it.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, cast

GB = 1000**3
DAY = 86400


# ------------------------------------------------------------------- paths
def rel_from_media_path(path: str, prefix: str) -> str | None:
    """A path as Jellyfin reports it ('/media/movies/X/x.mkv') to a pool-relative one ('movies/X/x.mkv').

    `prefix` is where Jellyfin sees the pool root ('/media'). Returns None for a path outside it."""
    p = path.replace("\\", "/")
    pre = prefix.replace("\\", "/").rstrip("/")
    if pre and not p.startswith(pre + "/"):
        return None
    return p[len(pre) :].lstrip("/")


def suggest_prefix(jellyfin_path: str, roots: Iterable[str]) -> str | None:
    """Doctor helper: given a path Jellyfin reports and the library roots, guess the pool prefix."""
    p = jellyfin_path.replace("\\", "/")
    for root in roots:
        i = p.find("/" + root.strip("/") + "/")
        if i >= 0:
            return p[:i]
    return None


_SEASON_RE = re.compile(r"^(?:season|series|s)\s*0*(\d+)$", re.I)


def season_number(dirname: str) -> int | None:
    if dirname.lower() in ("specials", "special"):
        return 0
    m = _SEASON_RE.match(dirname.strip())
    return int(m.group(1)) if m else None


def unit_of(
    rel: str, series_roots: Iterable[str], movie_roots: Iterable[str]
) -> tuple[str, str, str | None, int | None] | None:
    """Which movable unit a pool-relative file path belongs to, or None.

    Roots are pool-relative folders ('movies', 'library/tv'). Returns (unit_rel, kind, series_rel,
    season). A movie is its folder; an episode is its season folder. Loose files at a show's root or
    in a library root stay where they are."""
    parts = rel.split("/")
    for root in movie_roots:
        r = root.strip("/").split("/")
        n = len(r)
        if parts[:n] == r and len(parts) >= n + 2:
            return "/".join(parts[: n + 1]), "movie", None, None
    for root in series_roots:
        r = root.strip("/").split("/")
        n = len(r)
        if parts[:n] == r and len(parts) >= n + 3:
            season = season_number(parts[n + 1])
            if season is None:
                return None
            return "/".join(parts[: n + 2]), "season", "/".join(parts[: n + 1]), season
    return None


# ------------------------------------------------------------------- units
@dataclass
class Unit:
    rel: str
    kind: str  # "movie" | "season"
    series: str | None = None
    season: int | None = None
    ssd_bytes: int = 0
    hdd_bytes: int = 0
    held: bool = False  # a file has another hard link (a torrent still seeding it)
    oldest_mtime: float = 0.0
    newest_mtime: float = 0.0
    added: float | None = None
    last_played: float | None = None
    plays: int = 0
    recent_episodes: int = 0  # series-level: distinct episodes watched in the last 7 days
    playing: bool = False
    hot_reason: str | None = None
    viewers: dict[str, float] = field(default_factory=dict)  # Jellyfin user id -> when they last played this unit
    popularity: float = 0.0  # sum over viewers of 0.5 ** (days since they watched / half-life)
    recent_viewers: int = 0  # distinct people who watched it in the last 30 days

    @property
    def watched(self) -> bool:
        return self.last_played is not None


@dataclass
class Move:
    rel: str
    bytes: int
    why: str


@dataclass
class Plan:
    demote: list[Move] = field(default_factory=list)
    promote: list[Move] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    popular: list[dict[str, Any]] = field(default_factory=list)  # most popular titles right now


@dataclass(frozen=True)
class Settings:
    reserve: int  # bytes the SSDs keep free for new downloads
    headroom: int  # demote down to reserve + headroom free
    min_age_days: int = 14  # a title stays on the SSD at least this long
    pin_days: int = 30  # after coming back to the SSD, not demoted for this long
    promote_episodes: int = 2  # this many episodes watched in 7 days = binge
    hot_plays: int = 2  # a movie played this often in 30 days is hot
    max_bytes: int = 600 * GB  # most that moves in one run
    popular_viewers: int = 2  # this many different people in 30 days makes a title popular
    half_life_days: float = 14.0  # a viewer counts half as much for every 14 days since they watched


@dataclass(slots=True)
class Entry:
    """One library file, as seen on the disks."""

    size: int
    ssd: bool
    hdd: bool
    nlink: int
    mtime: float


@dataclass
class Watch:
    """What Jellyfin says about one file, merged across all users."""

    last_played: float | None = None
    plays: int = 0
    created: float | None = None
    viewers: dict[str, float] = field(default_factory=dict)  # user id -> that person's last play of this file


def units_from_files(
    files: dict[str, Entry], series_roots: Iterable[str], movie_roots: Iterable[str]
) -> dict[str, Unit]:
    """Total up each movable unit from the file index."""
    units: dict[str, Unit] = {}
    for rel, e in files.items():
        found = unit_of(rel, series_roots, movie_roots)
        if not found:
            continue
        unit_rel, kind, series, season = found
        u = units.setdefault(unit_rel, Unit(unit_rel, kind, series, season))
        if e.ssd:
            u.ssd_bytes += e.size
        if e.hdd and not e.ssd:
            u.hdd_bytes += e.size
        if e.nlink > 1:
            u.held = True
        u.oldest_mtime = e.mtime if not u.oldest_mtime else min(u.oldest_mtime, e.mtime)
        u.newest_mtime = max(u.newest_mtime, e.mtime)
    return units


# -------------------------------------------------------------- watch data
def apply_watch(
    units: dict[str, Unit],
    watch: dict[str, Watch],
    playing_paths: set[str],
    series_roots: Iterable[str],
    movie_roots: Iterable[str],
    now: float,
) -> None:
    """Fold per-file watch data into the units. `watch` is keyed by pool-relative path."""
    recent_by_series: dict[str | None, set[str]] = {}
    for rel, w in watch.items():
        found = unit_of(rel, series_roots, movie_roots)
        if not found or found[0] not in units:
            continue
        u = units[found[0]]
        if w.last_played is not None:
            u.last_played = max(u.last_played or 0, w.last_played)
        u.plays += w.plays
        for uid, ts in w.viewers.items():
            u.viewers[uid] = max(u.viewers.get(uid, 0), ts)
        if w.created is not None:
            u.added = w.created if u.added is None else min(u.added, w.created)
        if u.kind == "season" and w.last_played is not None and now - w.last_played <= 7 * DAY:
            recent_by_series.setdefault(u.series, set()).add(rel)
    for rel in playing_paths:
        found = unit_of(rel, series_roots, movie_roots)
        if found and found[0] in units:
            units[found[0]].playing = True
    for u in units.values():
        if u.added is None:
            u.added = u.oldest_mtime or None
        if u.kind == "season":
            u.recent_episodes = len(recent_by_series.get(u.series, ()))


def popularity_of(viewers: dict[str, float], now: float, half_life_days: float) -> float:
    """Each person counts once, worth less the longer ago they last watched."""
    hl = max(half_life_days, 0.1)
    return cast(float, sum(0.5 ** (max(0.0, now - ts) / DAY / hl) for ts in viewers.values()))


def mark_hot(units: dict[str, Unit], s: Settings, now: float) -> None:
    """Decide which units count as 'being watched'."""
    by_series: dict[str | None, list[Unit]] = {}
    for u in units.values():
        u.hot_reason = None
        u.popularity = popularity_of(u.viewers, now, s.half_life_days)
        u.recent_viewers = sum(1 for ts in u.viewers.values() if now - ts <= 30 * DAY)
        if u.kind == "movie":
            if u.plays >= s.hot_plays and u.last_played and now - u.last_played <= 30 * DAY:
                u.hot_reason = f"played {u.plays} times this month"
            elif u.recent_viewers >= s.popular_viewers:
                u.hot_reason = f"{u.recent_viewers} people watched it this month"
        else:
            by_series.setdefault(u.series, []).append(u)
    for seasons in by_series.values():
        seasons.sort(key=lambda u: cast(int, u.season))
        watching = [u for u in seasons if u.playing]
        if seasons[0].recent_episodes >= s.promote_episodes or watching:
            current = max(seasons, key=lambda u: u.last_played or 0)
            if watching:
                current = watching[0]
            why = (
                f"{seasons[0].recent_episodes} episodes watched this week"
                if seasons[0].recent_episodes
                else "being watched now"
            )
            for u in seasons:
                if u.season == current.season:
                    u.hot_reason = why
                elif u.season == cast(int, current.season) + 1:
                    u.hot_reason = "next season of " + why
                elif current.season == 0 and u.season == 1:
                    u.hot_reason = why
    # Popular with several people: the season they watched, and the next one, even if nobody binged it.
    for seasons in by_series.values():
        for u in seasons:
            if u.recent_viewers >= s.popular_viewers:
                if not u.hot_reason:
                    u.hot_reason = f"{u.recent_viewers} people watched it this month"
                for n in seasons:
                    if n.season == cast(int, u.season) + 1 and not n.hot_reason:
                        n.hot_reason = f"next season of a show {u.recent_viewers} people watched this month"


def _age_text(ts: float | None, now: float) -> str:
    return f"{max(0, int((now - ts) / DAY))} days ago" if ts else "unknown"


# --------------------------------------------------------------- the plan
def plan(
    units: dict[str, Unit], ssd_free: int, s: Settings, now: float, cooldown: dict[str, dict[str, Any]] | None = None
) -> Plan:
    """What to move tonight. `cooldown` is {unit: {"dir": "promote"|"demote", "at": ts}} from the state file."""
    cooldown = cooldown or {}
    out = Plan()
    target_free = s.reserve + s.headroom

    def promoted_recently(u: Unit) -> bool:
        c = cooldown.get(u.rel)
        return bool(c and c.get("dir") == "promote" and now - c.get("at", 0) < s.pin_days * DAY)

    # ---- who could come to the SSD (most popular first)
    promos = [u for u in units.values() if u.hot_reason and u.hdd_bytes > 0 and not u.held and not u.playing]
    promos.sort(key=lambda u: (-u.popularity, -(u.last_played or 0)))
    # ---- who could leave the SSD (never watched, then least popular, then longest unwatched)
    cold = [
        u
        for u in units.values()
        if u.ssd_bytes > 0
        and not u.hot_reason
        and not u.playing
        and not u.held
        and not promoted_recently(u)
        and u.added
        and now - u.added >= s.min_age_days * DAY
    ]
    cold.sort(key=lambda u: (1 if u.watched else 0, u.popularity, u.last_played or u.added or 0))
    demotable = sum(u.ssd_bytes for u in cold)
    top = sorted((u for u in units.values() if u.popularity > 0.05), key=lambda u: -u.popularity)[:10]
    out.popular = [
        {
            "unit": u.rel,
            "viewers": u.recent_viewers,
            "score": round(u.popularity, 2),
            "where": "ssd" if u.hdd_bytes == 0 and u.ssd_bytes > 0 else "hdd" if u.ssd_bytes == 0 else "mixed",
        }
        for u in top
    ]

    budget = s.max_bytes
    proj_free = ssd_free
    for u in promos:
        if u.hdd_bytes > budget:
            out.notes.append(f"{u.rel}: skipped for now (over tonight's move limit)")
            continue
        # it fits if, after demoting everything demotable, the SSD still keeps its reserve
        if proj_free - u.hdd_bytes + demotable < s.reserve:
            out.notes.append(f"{u.rel}: wanted on the SSD but there isn't room even after moving older titles")
            continue
        out.promote.append(Move(u.rel, u.hdd_bytes, cast(str, u.hot_reason)))
        proj_free -= u.hdd_bytes
        budget -= u.hdd_bytes

    need = target_free - proj_free
    if need > 0:
        for u in cold:
            if need <= 0 or u.ssd_bytes > budget:
                continue
            why = (
                ("never watched, added " + _age_text(u.added, now))
                if not u.watched
                else ("last watched " + _age_text(u.last_played, now))
            )
            out.demote.append(Move(u.rel, u.ssd_bytes, why))
            need -= u.ssd_bytes
            budget -= u.ssd_bytes
        if need > 0:
            out.notes.append("the SSDs are still short of their target after moving everything that is old enough")
    elif not out.promote:
        out.notes.append("the SSDs have room: nothing needs to move to the HDDs yet")
    return out


def effective_free(branch_free: Iterable[tuple[int, int]], reserve: int) -> int:
    """Free space the planner should count on the SSD tier.

    `branch_free` is [(free_bytes, filesystem_id)] for each SSD branch. mergerfs stops sending new
    files to a branch once it has less than the reserve free, so each branch's last `reserve` bytes
    are not room for new files. Counting only what is above it (plus one reserve, the single-SSD
    meaning of 'free') makes 'reserve + headroom free' reachable with several SSDs; a plain sum would
    stay above it forever and nothing would be demoted. Branches on one filesystem count once."""
    seen, usable = set(), 0
    for free, fs in branch_free:
        if fs in seen:
            continue
        seen.add(fs)
        usable += max(0, free - reserve)
    return usable + reserve if seen else 0
