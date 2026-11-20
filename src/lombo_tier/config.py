"""Settings, read once from environment variables (see .env.example).

Every setting has a LOMBO_TIER_ prefix so it never collides with another container's variables.
A bad value stops the app at start-up with a message that names the variable.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlparse

from .core import GB, Settings

PREFIX = "LOMBO_TIER_"


class ConfigError(Exception):
    """A setting is missing or invalid. The message says which one and what to do."""


@dataclass(frozen=True)
class Config:
    jellyfin_url: str
    jellyfin_api_key: str
    path_prefix: str  # where Jellyfin sees the pool root, e.g. /media
    ssd_branches: tuple[str, ...]
    hdd_branches: tuple[str, ...]
    movie_roots: tuple[str, ...]
    series_roots: tuple[str, ...]
    data_dir: str
    settings: Settings  # the planner's rules (reserve, headroom, ages...)
    max_run_minutes: int
    wait_quiet_minutes: int
    dest_margin: int  # bytes a destination disk must keep free after a move
    check_mounts: bool
    disk_marker: str
    hdd_index_max_days: float
    run_hour: int
    webhook_url: str | None
    ping_url: str | None
    dry_run: bool

    @property
    def roots(self) -> tuple[str, ...]:
        return self.movie_roots + self.series_roots

    @property
    def state_path(self) -> str:
        return os.path.join(self.data_dir, "state.json")

    @property
    def hdd_index_path(self) -> str:
        return os.path.join(self.data_dir, "hdd-index.json")


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def from_env(env: dict[str, str]) -> Config:
    def get(name: str, default: str = "") -> str:
        return (env.get(PREFIX + name) or default).strip()

    def number(
        name: str,
        default: str,
        kind: Callable[[str], Any] = float,
        minimum: float = 0,
        strict: bool = False,
    ) -> Any:
        raw = get(name, default)
        try:
            value = kind(raw)
        except ValueError:
            raise ConfigError(f"{PREFIX}{name} must be a number (got {raw!r})") from None
        if value < minimum or (strict and value == minimum):
            raise ConfigError(f"{PREFIX}{name} must be {'more than' if strict else 'at least'} {minimum} (got {raw})")
        return value

    def url(name: str, required: bool) -> str | None:
        raw = get(name).rstrip("/")
        if not raw:
            if required:
                raise ConfigError(f"{PREFIX}{name} must be a full URL such as http://jellyfin:8096")
            return None
        parsed = urlparse(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigError(
                f"{PREFIX}{name} must start with http:// or https:// (got {raw!r})"
                + ("" if required else ", or be empty")
            )
        return raw

    def folders(name: str, default: str = "", absolute: bool = False) -> tuple[str, ...]:
        parts = [p.strip() for p in get(name, default).split(",") if p.strip()]
        out = []
        for p in parts:
            if absolute:
                if not p.startswith("/"):
                    raise ConfigError(f"{PREFIX}{name}: {p!r} must be an absolute path such as /mnt/ssd1")
                out.append(p.rstrip("/") or "/")
            else:
                clean = p.strip("/")
                if not clean or ".." in clean.split("/"):
                    raise ConfigError(f"{PREFIX}{name}: {p!r} must be a folder inside the pool such as movies")
                out.append(clean)
        if len(set(out)) != len(out):
            raise ConfigError(f"{PREFIX}{name} lists the same folder twice")
        return tuple(out)

    jellyfin = cast(str, url("JELLYFIN_URL", True))  # required=True: url() never returns None here
    key = get("JELLYFIN_API_KEY")
    if not key or key == "changeme":
        raise ConfigError(f"{PREFIX}JELLYFIN_API_KEY is not set. Create one in Jellyfin: Dashboard > API Keys.")
    prefix = get("JELLYFIN_PATH_PREFIX").rstrip("/\\")
    if not prefix:
        raise ConfigError(
            f"{PREFIX}JELLYFIN_PATH_PREFIX is not set. It is where Jellyfin sees the pool root, "
            "for example /media (run `lombo-tier doctor` to get a suggestion)."
        )

    ssd = folders("SSD_BRANCHES", absolute=True)
    hdd = folders("HDD_BRANCHES", absolute=True)
    if not ssd:
        raise ConfigError(f"{PREFIX}SSD_BRANCHES is not set: list the folder of each SSD in the pool, e.g. /mnt/ssd1")
    if not hdd:
        raise ConfigError(
            f"{PREFIX}HDD_BRANCHES is not set: list the folder of each HDD in the pool, e.g. /mnt/hdd1,/mnt/hdd2"
        )
    both = set(ssd) & set(hdd)
    if both:
        raise ConfigError(f"{PREFIX}SSD_BRANCHES and {PREFIX}HDD_BRANCHES both list {sorted(both)[0]}")
    movie_roots = folders("MOVIE_ROOTS", "movies")
    series_roots = folders("SERIES_ROOTS", "tv")
    if set(movie_roots) & set(series_roots):
        raise ConfigError(f"{PREFIX}MOVIE_ROOTS and {PREFIX}SERIES_ROOTS must not share a folder")

    settings = Settings(
        reserve=int(number("RESERVE_GB", "40") * GB),
        headroom=int(number("HEADROOM_GB", "40") * GB),
        min_age_days=number("MIN_AGE_DAYS", "14", int),
        pin_days=number("PIN_DAYS", "30", int),
        promote_episodes=number("PROMOTE_EPISODES", "2", int, 1),
        hot_plays=number("HOT_PLAYS", "2", int, 1),
        max_bytes=int(number("MAX_GB_PER_RUN", "600", float, 0, strict=True) * GB),
        popular_viewers=number("POPULAR_VIEWERS", "2", int, 1),
        half_life_days=number("HALF_LIFE_DAYS", "14", float, 0, strict=True),
    )
    marker = get("DISK_MARKER")
    if "/" in marker or marker in {".", ".."}:
        raise ConfigError(f"{PREFIX}DISK_MARKER must be a folder name, not a path (got {marker!r})")
    run_hour = number("RUN_HOUR", "4", int)
    if run_hour > 23:
        raise ConfigError(f"{PREFIX}RUN_HOUR must be 0 to 23 (got {run_hour})")

    return Config(
        jellyfin_url=jellyfin,
        jellyfin_api_key=key,
        path_prefix=prefix,
        ssd_branches=ssd,
        hdd_branches=hdd,
        movie_roots=movie_roots,
        series_roots=series_roots,
        data_dir=get("DATA_DIR", "/data"),
        settings=settings,
        max_run_minutes=number("MAX_RUN_MINUTES", "240", int, 1),
        wait_quiet_minutes=number("WAIT_QUIET_MINUTES", "30", int),
        dest_margin=int(number("DEST_MARGIN_GB", "20") * GB),
        check_mounts=_truthy(get("CHECK_MOUNTS", "true")),
        disk_marker=marker,
        hdd_index_max_days=number("HDD_INDEX_MAX_DAYS", "7"),
        run_hour=run_hour,
        webhook_url=url("WEBHOOK_URL", False),
        ping_url=url("PING_URL", False),
        # Safe by default: nothing moves until the person sets DRY_RUN=false.
        dry_run=_truthy(get("DRY_RUN", "true")),
    )
