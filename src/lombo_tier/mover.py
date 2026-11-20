"""One run: look at the disks and Jellyfin, make the plan, and (unless it is a dry run) move things.

Every safety check lives here or in disks.py: nothing moves while someone is watching, a move stops
the moment someone starts, nothing moves if a disk is not mounted or the state file cannot be read,
and a copy is hash-verified before the original is removed.
"""

import logging
import os
import threading
import time
import urllib.request
from collections.abc import Callable
from typing import Any

from . import core, disks, notify, watch
from . import state as statemod
from .config import Config
from .jellyfin import Jellyfin

log = logging.getLogger("lombotier.mover")


# ------------------------------------------------------------------- plan
def make_plan(
    cfg: Config, jf: Jellyfin, now: float | None = None, files: dict[str, core.Entry] | None = None
) -> tuple[core.Plan, dict[str, core.Unit], int]:
    """Scan (or reuse `files`), ask Jellyfin, plan. Read-only. Returns (plan, units, ssd_free_bytes)."""
    now = time.time() if now is None else now
    if files is None:
        files = disks.scan_files(
            cfg.ssd_branches,
            cfg.hdd_branches,
            cfg.roots,
            cfg.hdd_index_path,
            cfg.hdd_index_max_days,
            marker=cfg.disk_marker,
        )
    units = core.units_from_files(files, cfg.series_roots, cfg.movie_roots)
    core.apply_watch(
        units,
        watch.collect_watch(jf, cfg.path_prefix),
        watch.current_playing(jf, cfg.path_prefix),
        cfg.series_roots,
        cfg.movie_roots,
        now,
    )
    core.mark_hot(units, cfg.settings, now)
    free = core.effective_free(disks.branch_free(cfg.ssd_branches), cfg.settings.reserve)
    cooldown = statemod.load(cfg.state_path).get("cooldown", {})
    return core.plan(units, free, cfg.settings, now, cooldown), units, free


def plan_json(p: core.Plan) -> dict[str, Any]:
    return {
        "demote": [m.__dict__ for m in p.demote],
        "promote": [m.__dict__ for m in p.promote],
        "notes": p.notes,
        "popular": p.popular,
    }


# ------------------------------------------------------------------ watcher
class PlayingWatcher(threading.Thread):
    """Polls Jellyfin while files move. If Jellyfin cannot be asked, assume someone is watching."""

    def __init__(self, cfg: Config, jf: Jellyfin, interval: float = 30) -> None:
        super().__init__(daemon=True)
        self.cfg, self.jf, self.interval = cfg, jf, interval
        self.flag = False
        self._stop_event = threading.Event()

    def run(self) -> None:
        failed = False
        while not self._stop_event.wait(self.interval):
            try:
                self.flag = bool(watch.current_playing(self.jf, self.cfg.path_prefix))
                failed = False
            except Exception as exc:  # noqa: BLE001  any failure means "assume someone is watching"
                self.flag = True
                if not failed:
                    log.warning("can't ask Jellyfin who is watching, assuming someone is: %s", exc)
                failed = True

    def stop(self) -> None:
        self._stop_event.set()


# --------------------------------------------------------------------- run
def _new_result(started: float) -> dict[str, Any]:
    return {"started": started, "finished": None, "note": "", "demoted": [], "promoted": [], "skipped": []}


def _precheck(cfg: Config) -> str | None:
    """A reason not to move anything at all, or None."""
    if cfg.check_mounts:
        missing = disks.unmounted_branches(cfg.ssd_branches + cfg.hdd_branches, cfg.disk_marker)
        if missing:
            names = ", ".join(missing)
            log.error("ALERT: disk(s) not mounted: %s. Not moving anything.", names)
            return (
                f"disk(s) not mounted: {names}. Not moving anything until they are. "
                "A branch that is just an empty folder would fill the boot disk."
            )
    return None


def run(
    cfg: Config,
    jf: Jellyfin,
    dry_run: bool | None = None,
    wait: bool = True,
    sleep: Callable[[float], object] = time.sleep,
    poll: float = 30,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """One run. `dry_run` defaults to the DRY_RUN setting. Returns the result dict (also saved to the state file)."""
    dry_run = cfg.dry_run if dry_run is None else dry_run
    started = now()
    result = _new_result(started)
    bad = statemod.problem(cfg.state_path)
    if bad:
        result.update(
            finished=now(),
            failed=True,
            note=(
                f"the state file can't be read ({bad}): not moving anything and leaving it as it is; "
                f"fix or move {cfg.state_path} aside"
            ),
        )
        log.error("ALERT: %s", result["note"])
        return result
    state = statemod.load(cfg.state_path)

    def finish(note: str, failed: bool = False) -> dict[str, Any]:
        result["note"], result["finished"] = note, now()
        if failed:
            result["failed"] = True
        if not dry_run:
            state["last_run"] = result
            statemod.save(cfg.state_path, state)
        return result

    if why := _precheck(cfg):
        return finish(why, failed=True)
    p, _units, _free = make_plan(cfg, jf, started)
    if (p.demote or p.promote) and not dry_run:
        # About to move files, so the HDDs will be awake anyway: plan again from a fresh walk of them.
        fresh = disks.scan_files(
            cfg.ssd_branches,
            cfg.hdd_branches,
            cfg.roots,
            cfg.hdd_index_path,
            cfg.hdd_index_max_days,
            fresh_hdd=True,
            marker=cfg.disk_marker,
        )
        p, _units, _free = make_plan(cfg, jf, started, files=fresh)
    state["plan"] = plan_json(p)
    result["plan"] = plan_json(p)
    if dry_run:
        return finish("dry run: nothing was moved")
    if not p.demote and not p.promote:
        return finish("; ".join(p.notes) or "nothing to move")

    # Wait for a quiet moment: nobody watching.
    quiet_deadline = started + cfg.wait_quiet_minutes * 60
    while wait and watch.current_playing(jf, cfg.path_prefix):
        if now() > quiet_deadline:
            return finish("people were watching the whole time: nothing moved, will try again next time")
        sleep(60)

    watcher = PlayingWatcher(cfg, jf, poll)
    watcher.start()
    deadline = started + cfg.max_run_minutes * 60
    try:
        for kind, moves, src, dst in (
            ("demoted", p.demote, cfg.ssd_branches, cfg.hdd_branches),
            ("promoted", p.promote, cfg.hdd_branches, cfg.ssd_branches),
        ):
            for m in moves:
                if watcher.flag:
                    result["skipped"].append({"unit": m.rel, "why": "someone started watching"})
                    continue
                try:
                    r = disks.move_unit(m.rel, src, dst, lambda: watcher.flag, deadline, cfg.dest_margin)
                except OSError as exc:  # disk full, file vanished, permissions: skip this unit, keep going
                    log.warning("moving %s failed: %s", m.rel, exc)
                    result["skipped"].append({"unit": m.rel, "why": f"{type(exc).__name__}: {exc}"[:200]})
                    continue
                if r["moved"]:
                    result[kind].append({"unit": m.rel, "bytes": r["bytes"]})
                    state.setdefault("cooldown", {})[m.rel] = {
                        "dir": "promote" if kind == "promoted" else "demote",
                        "at": now(),
                    }
                if r["skipped"]:
                    result["skipped"].append({"unit": m.rel, "why": r["skipped"]})
                    if "out of time" in r["skipped"]:
                        break
    except Exception as exc:
        log.exception("run failed")
        return finish(
            f"stopped by an error ({type(exc).__name__}: {exc}); progress so far was saved"[:300], failed=True
        )
    finally:
        watcher.stop()
        if result["demoted"] or result["promoted"]:
            # The moves changed what is on the HDDs; walk them once more while they are awake so the saved
            # file list matches the disks.
            try:
                disks.scan_files(
                    cfg.ssd_branches,
                    cfg.hdd_branches,
                    cfg.roots,
                    cfg.hdd_index_path,
                    cfg.hdd_index_max_days,
                    fresh_hdd=True,
                    marker=cfg.disk_marker,
                )
            except Exception as exc:  # noqa: BLE001  a failed re-index is not a failed run
                log.warning("HDD file list not refreshed after the moves: %s", exc)
    return finish("ok")


# ------------------------------------------------------------- manual move
def resolve_units(cfg: Config, names: list[str]) -> list[str]:
    """Turn `move` names into unit paths that exist on the SSD branches. Raises ValueError on a miss or a tie."""
    found = []
    for name in names:
        name = name.strip().strip("/")
        cands = set()
        for b in cfg.ssd_branches:
            if os.path.isdir(os.path.join(b, name)):
                cands.add(name)
            for root in cfg.roots:
                rel = f"{root}/{name}"
                if os.path.isdir(os.path.join(b, rel)):
                    cands.add(rel)
        if len(cands) != 1:
            raise ValueError(
                f"{name!r}: "
                + ("not found on the SSDs" if not cands else "matches several: " + ", ".join(sorted(cands)))
            )
        rel = next(iter(cands))
        if core.unit_of(rel + "/x.mkv", cfg.series_roots, cfg.movie_roots) is None:
            raise ValueError(f"{name!r}: {rel} is not a movie or show-season folder")
        found.append(rel)
    return found


def run_move(
    cfg: Config,
    jf: Jellyfin,
    names: list[str],
    dry_run: bool | None = None,
    wait: bool = True,
    sleep: Callable[[float], object] = time.sleep,
    poll: float = 30,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Move the named titles from the SSDs to the HDDs now, with the same safety checks as a run."""
    dry_run = cfg.dry_run if dry_run is None else dry_run
    started = now()
    result = _new_result(started)
    bad = statemod.problem(cfg.state_path)
    if bad:
        result.update(finished=now(), failed=True, note=f"the state file can't be read ({bad}): not moving anything")
        return result
    state = statemod.load(cfg.state_path)

    def finish(note: str, failed: bool = False) -> dict[str, Any]:
        result["note"], result["finished"] = note, now()
        if failed:
            result["failed"] = True
        if not dry_run:
            state["last_run"] = result
            statemod.save(cfg.state_path, state)
        return result

    if why := _precheck(cfg):
        return finish(why, failed=True)
    units = resolve_units(cfg, names)
    if dry_run:
        result["would_move_to_hdd"] = units
        return finish("dry run: nothing was moved")
    quiet_deadline = started + cfg.wait_quiet_minutes * 60
    while wait and watch.current_playing(jf, cfg.path_prefix):
        if now() > quiet_deadline:
            return finish("people were watching the whole time: nothing moved")
        sleep(60)
    watcher = PlayingWatcher(cfg, jf, poll)
    watcher.start()
    try:
        for rel in units:
            if watcher.flag:
                result["skipped"].append({"unit": rel, "why": "someone started watching"})
                continue
            try:
                r = disks.move_unit(
                    rel, cfg.ssd_branches, cfg.hdd_branches, lambda: watcher.flag, None, cfg.dest_margin
                )
            except OSError as exc:
                log.warning("moving %s failed: %s", rel, exc)
                result["skipped"].append({"unit": rel, "why": f"{type(exc).__name__}: {exc}"[:200]})
                continue
            if r["moved"]:
                result["demoted"].append({"unit": rel, "bytes": r["bytes"]})
                state.setdefault("cooldown", {})[rel] = {"dir": "demote", "at": now()}
            if r["skipped"]:
                result["skipped"].append({"unit": rel, "why": r["skipped"]})
    except Exception as exc:
        log.exception("manual move failed")
        return finish(
            f"stopped by an error ({type(exc).__name__}: {exc}); progress so far was saved"[:300], failed=True
        )
    finally:
        watcher.stop()
        if result["demoted"]:
            try:
                disks.scan_files(
                    cfg.ssd_branches,
                    cfg.hdd_branches,
                    cfg.roots,
                    cfg.hdd_index_path,
                    cfg.hdd_index_max_days,
                    fresh_hdd=True,
                    marker=cfg.disk_marker,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("HDD file list not refreshed after the moves: %s", exc)
    return finish("manual move: ok")


# ---------------------------------------------------------- reporting
def summary(result: dict[str, Any]) -> str:
    gb: Callable[[list[dict[str, Any]]], float] = lambda items: sum(i["bytes"] for i in items) / core.GB  # noqa: E731
    lines = [f"LomboTier: {result['note']}"]
    if result["demoted"]:
        lines.append(f"Moved to HDD: {len(result['demoted'])} title(s), {gb(result['demoted']):.1f} GB")
    if result["promoted"]:
        lines.append(f"Moved back to SSD: {len(result['promoted'])} title(s), {gb(result['promoted']):.1f} GB")
    for s in result["skipped"]:
        lines.append(f"Skipped {s['unit']}: {s['why']}")
    return "\n".join(lines)


def ping(base: str | None, failed: bool) -> None:
    """Tell a healthchecks-style service the run is over: GET base, or base/fail. No URL = off.
    Never raises and never waits long. The URL holds a secret, so it is never logged."""
    if not base:
        return
    try:
        url = base + ("/fail" if failed else "")
        urllib.request.urlopen(url, timeout=8).close()  # noqa: S310 - config.py only accepts http(s)
    except Exception as exc:  # noqa: BLE001
        log.warning("ping failed (%s)", type(exc).__name__)


def report(cfg: Config, result: dict[str, Any]) -> None:
    """After a real run: ping, and send a summary if something moved or it failed."""
    failed = bool(result.get("failed"))
    ping(cfg.ping_url, failed)
    if cfg.webhook_url and (failed or result["demoted"] or result["promoted"]):
        try:
            notify.send(cfg.webhook_url, summary(result))
        except notify.NotifyError as exc:
            log.warning("%s", exc)
