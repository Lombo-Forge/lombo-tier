"""One run: look at the disks and Jellyfin, make the plan, and (unless it is a dry run) move things.

Every safety check lives here or in disks.py: nothing moves while someone is watching, a move stops
the moment someone starts, nothing moves if a disk is not mounted or the state file, the pin list or the
conversion session file cannot be trusted, only one run or move happens at a time, and a copy is
hash-verified before the original is removed.
"""

import contextlib
import fcntl
import json
import logging
import os
import subprocess
import threading
import time
import urllib.request
from collections.abc import Callable, Sequence
from typing import Any

from . import core, disks, notify, pins, session, tiers, watch
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
    less = {b: r - cfg.settings.reserve for b, r in cfg.branch_reserve}
    free = core.effective_free(disks.branch_free(cfg.ssd_branches, less), cfg.settings.reserve)
    if bad := pins.problem(cfg.pin_file):
        # Never read a damaged pin list as an empty one: plan nothing.
        return core.Plan(notes=[f"the pin list can't be read ({bad}): standing down, nothing will move"]), units, free
    cooldown = statemod.load(cfg.state_path).get("cooldown", {})
    pinned = pinned_units(cfg, units)
    p = core.plan(units, free, cfg.settings, now, cooldown, pinned, gather_pinned=bool(cfg.pin_branch))
    if cfg.pin_branch:
        p.gather, notes = plan_gather(cfg, units, pinned)
        p.notes += notes
    return p, units, free


def pinned_units(cfg: Config, units: dict[str, core.Unit]) -> frozenset[str]:
    """The units covered by the pin list. Raises ValueError if the list is damaged."""
    titles = pins.load(cfg.pin_file)
    return frozenset(rel for rel in units if pins.is_pinned(rel, titles, cfg.roots))


def _bytes_on(branches: list[str], rel: str) -> int:
    total = 0
    for b in branches:
        for dirpath, _d, names in os.walk(os.path.join(b, rel)):
            for n in names:
                if not n.endswith(disks.PART_SUFFIX):
                    with contextlib.suppress(OSError):
                        total += os.path.getsize(os.path.join(dirpath, n))
    return total


def other_branches(cfg: Config) -> list[str]:
    """Every branch except the pin branch: where pinned titles are gathered from."""
    return [b for b in cfg.ssd_branches + cfg.hdd_branches if b != cfg.pin_branch]


def plan_gather(cfg: Config, units: dict[str, core.Unit], pinned: frozenset[str]) -> tuple[list[core.Move], list[str]]:
    """Pinned titles with files outside the pin branch, to be moved onto it. Nothing if the branch is not there."""
    if not pinned:
        return [], []
    branch = cfg.pin_branch
    mounted = disks.disk_mounted(branch, cfg.disk_marker) if cfg.check_mounts else os.path.isdir(branch)
    if not mounted:
        return [], [f"the pin branch {branch} is missing or not mounted: not moving pinned titles"]
    out: list[core.Move] = []
    notes: list[str] = []
    budget = cfg.settings.max_bytes
    for rel in sorted(pinned):
        u = units[rel]
        size = u.hdd_bytes + _bytes_on([b for b in cfg.ssd_branches if b != branch], rel)
        if size == 0:
            continue
        if u.held or u.playing:
            notes.append(f"{rel}: pinned, but it is {'playing' if u.playing else 'still seeding'}: not gathered now")
        elif size > budget:
            notes.append(f"{rel}: pinned, skipped for now (over the move limit)")
        else:
            out.append(core.Move(rel, size, f"pinned: gather onto {branch}"))
            budget -= size
    return out, notes


def plan_json(p: core.Plan) -> dict[str, Any]:
    return {
        "demote": [m.__dict__ for m in p.demote],
        "promote": [m.__dict__ for m in p.promote],
        "gather": [m.__dict__ for m in p.gather],
        "notes": p.notes,
        "popular": p.popular,
    }


# ------------------------------------------------------------------ watcher
class PlayingWatcher(threading.Thread):
    """Polls Jellyfin while files move. If Jellyfin cannot be asked, assume someone is watching.
    A conversion session that opens while files move stops them too."""

    def __init__(self, cfg: Config, jf: Jellyfin, interval: float = 30) -> None:
        super().__init__(daemon=True)
        self.cfg, self.jf, self.interval = cfg, jf, interval
        self.flag = False
        self.reason = "someone started watching"
        self._stop_event = threading.Event()

    def run(self) -> None:
        failed = False
        while not self._stop_event.wait(self.interval):
            try:
                playing = bool(watch.current_playing(self.jf, self.cfg.path_prefix))
                failed = False
            except Exception as exc:  # noqa: BLE001  any failure means "assume someone is watching"
                playing = True
                if not failed:
                    log.warning("can't ask Jellyfin who is watching, assuming someone is: %s", exc)
                failed = True
            held = session.check(self.cfg.session_file, time.time())[0] != session.CLOSED
            self.flag = playing or held
            self.reason = "someone started watching" if playing else "a conversion session opened"

    def stop(self) -> None:
        self._stop_event.set()


# --------------------------------------------------------------------- run
def _new_result(started: float) -> dict[str, Any]:
    return {
        "started": started,
        "finished": None,
        "note": "",
        "demoted": [],
        "promoted": [],
        "gathered": [],
        "skipped": [],
    }


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


def _stand_down(cfg: Config, now: float) -> tuple[str, bool] | None:
    """A reason to move nothing at all and leave the state file alone: (note, is_a_failure), or None."""
    if bad := pins.problem(cfg.pin_file):
        return f"the pin list can't be read ({bad}): not moving anything; fix or move {cfg.pin_file} aside", True
    status, detail = session.check(cfg.session_file, now)
    if status == session.UNREADABLE:
        return (
            f"the conversion session file can't be trusted ({detail}): not moving anything; "
            f"fix or remove {cfg.session_file}",
            True,
        )
    if status == session.OPEN:
        return f"a conversion session is open ({detail}): not moving anything until it closes", False
    return None


def _acquire_lock(path: str) -> int:
    """Take the run lock: an advisory lock on a file in the data folder. It goes away with the process, so a
    crash never leaves it stuck. Raises BlockingIOError when someone holds it, OSError if the file can't be made."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise
    return fd


def _locked(cfg: Config, started: float, dry_run: bool, work: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run `work` under the run lock. A dry run changes nothing, so it does not need the lock."""
    if dry_run:
        return work()
    try:
        fd = _acquire_lock(cfg.lock_path)
    except BlockingIOError:
        log.warning("another run or move is in progress: not starting")
        return {**_new_result(started), "busy": True, "note": "another run or move is in progress: not starting"}
    except OSError as exc:
        return {
            **_new_result(started),
            "failed": True,
            "note": f"can't take the run lock {cfg.lock_path} ({exc.__class__.__name__}): not moving anything",
        }
    try:
        return work()
    finally:
        os.close(fd)


def _publish_tiers(cfg: Config, units: dict[str, core.Unit], now: float) -> None:
    """Write tiers.json. Failing to write it never fails a run."""
    try:
        titles = pins.load(cfg.pin_file)
        tiers.write(cfg.tiers_path, tiers.build(units.values(), titles, cfg.roots, now))
    except (OSError, ValueError) as exc:
        log.warning("tiers.json not written: %s", exc)


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
    return _locked(cfg, now(), dry_run, lambda: _run(cfg, jf, dry_run, wait, sleep, poll, now))


def _post_move(cfg: Config, result: dict[str, Any]) -> str | None:
    """Run the optional post-move command once. Returns a short problem text, or None when it ran fine or is unset."""
    if not cfg.post_move_command:
        return None
    moved = {k: [i["unit"] for i in result[k]] for k in ("demoted", "promoted", "gathered")}
    env = {**os.environ, "LOMBO_TIER_MOVED_JSON": json.dumps(moved)}
    try:
        done = subprocess.run(  # noqa: S603 - no shell; the command is the owner's own setting
            list(cfg.post_move_command),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        return f"post-move command failed ({type(err).__name__})"
    return None if done.returncode == 0 else f"post-move command failed (exit {done.returncode})"


def _hook(cfg: Config, result: dict[str, Any], dry_run: bool) -> None:
    """After tiers.json is written: run the post-move command if something really moved. A failure is a
    note on the result and a log line, never a failed run."""
    if dry_run or not (result["demoted"] or result["promoted"] or result["gathered"]):
        return
    problem = _post_move(cfg, result)
    if problem:
        log.warning("%s", problem)
        result["note"] = f"{result['note']}; {problem}" if result["note"] else problem


def _run(
    cfg: Config,
    jf: Jellyfin,
    dry_run: bool,
    wait: bool,
    sleep: Callable[[float], object],
    poll: float,
    now: Callable[[], float],
) -> dict[str, Any]:
    started = now()
    result = _new_result(started)
    units: dict[str, core.Unit] = {}
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
    if stand := _stand_down(cfg, started):
        result.update(finished=now(), note=stand[0])
        if stand[1]:
            result["failed"] = True
            log.error("ALERT: %s", stand[0])
        return result
    state = statemod.load(cfg.state_path)

    def finish(note: str, failed: bool = False) -> dict[str, Any]:
        result["note"], result["finished"] = note, now()
        if failed:
            result["failed"] = True
        if units:
            _publish_tiers(cfg, units, now())
        _hook(cfg, result, dry_run)
        if not dry_run:
            state["last_run"] = result
            statemod.save(cfg.state_path, state)
        return result

    if why := _precheck(cfg):
        return finish(why, failed=True)
    p, units, _free = make_plan(cfg, jf, started)
    if (p.demote or p.promote or p.gather) and not dry_run:
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
        p, units, _free = make_plan(cfg, jf, started, files=fresh)
    state["plan"] = plan_json(p)
    result["plan"] = plan_json(p)
    if dry_run:
        return finish("dry run: nothing was moved")
    if not p.demote and not p.promote and not p.gather:
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
    not_mounted = ""
    try:
        for kind, moves, src, dst in (
            ("demoted", p.demote, cfg.ssd_branches, cfg.hdd_branches),
            ("promoted", p.promote, cfg.hdd_branches, cfg.ssd_branches),
            ("gathered", p.gather, other_branches(cfg), [cfg.pin_branch]),
        ):
            for m in moves:
                if watcher.flag:
                    result["skipped"].append({"unit": m.rel, "why": watcher.reason})
                    continue
                try:
                    r = disks.move_unit(
                        m.rel,
                        src,
                        dst,
                        lambda: watcher.flag,
                        deadline,
                        cfg.dest_margin,
                        marker=cfg.disk_marker if cfg.check_mounts else None,
                        reserves=dict(cfg.branch_reserve),
                    )
                except OSError as exc:  # disk full, file vanished, permissions: skip this unit, keep going
                    log.warning("moving %s failed: %s", m.rel, exc)
                    result["skipped"].append({"unit": m.rel, "why": f"{type(exc).__name__}: {exc}"[:200]})
                    continue
                if r["moved"]:
                    result[kind].append({"unit": m.rel, "bytes": r["bytes"]})
                if kind != "gathered" and r["moved"] and not r["skipped"]:  # a half-moved unit gets no cooldown
                    state.setdefault("cooldown", {})[m.rel] = {
                        "dir": "promote" if kind == "promoted" else "demote",
                        "at": now(),
                    }
                if r["skipped"]:
                    result["skipped"].append({"unit": m.rel, "why": r["skipped"]})
                    if r["skipped"].startswith(disks.NOT_MOUNTED):
                        log.error("ALERT: %s. Stopping the run.", r["skipped"])
                        not_mounted = r["skipped"]
                        break
                    if "out of time" in r["skipped"]:
                        break
            if not_mounted:
                break
    except Exception as exc:
        log.exception("run failed")
        return finish(
            f"stopped by an error ({type(exc).__name__}: {exc}); progress so far was saved"[:300], failed=True
        )
    finally:
        watcher.stop()
        if result["demoted"] or result["promoted"] or result["gathered"]:
            # The moves changed what is on the HDDs; walk them once more while they are awake so the saved
            # file list matches the disks.
            try:
                after = disks.scan_files(
                    cfg.ssd_branches,
                    cfg.hdd_branches,
                    cfg.roots,
                    cfg.hdd_index_path,
                    cfg.hdd_index_max_days,
                    fresh_hdd=True,
                    marker=cfg.disk_marker,
                )
                units = core.units_from_files(after, cfg.series_roots, cfg.movie_roots)
            except Exception as exc:  # noqa: BLE001  a failed re-index is not a failed run
                log.warning("HDD file list not refreshed after the moves: %s", exc)
    if not_mounted:
        return finish(f"{not_mounted}. Stopped the run; nothing else was moved.", failed=True)
    return finish("ok")


# ------------------------------------------------------------- manual move
def resolve_title(branches: Sequence[str], cfg: Config, name: str, where: str) -> str:
    """One title folder (pool-relative) from a name or a library path. Raises ValueError on a miss or a tie."""
    name = name.strip().strip("/")
    if not name or ".." in name.split("/"):
        raise ValueError(f"{name!r}: an empty name or '..' is not allowed in a title name")
    cands = set()
    for b in branches:
        if os.path.isdir(os.path.join(b, name)):
            cands.add(name)
        for root in cfg.roots:
            rel = f"{root}/{name}"
            if os.path.isdir(os.path.join(b, rel)):
                cands.add(rel)
    if len(cands) != 1:
        raise ValueError(
            f"{name!r}: " + (f"not found {where}" if not cands else "matches several: " + ", ".join(sorted(cands)))
        )
    return next(iter(cands))


def resolve_units(cfg: Config, names: list[str]) -> list[str]:
    """Turn `move` names into unit paths that exist on the SSD branches. Raises ValueError on a miss or a tie."""
    found = []
    for name in names:
        rel = resolve_title(list(cfg.ssd_branches), cfg, name, "on the SSDs")
        if core.unit_of(rel + "/x.mkv", cfg.series_roots, cfg.movie_roots) is None:
            raise ValueError(f"{name!r}: {rel} is not a movie or show-season folder")
        found.append(rel)
    return found


def resolve_pin(cfg: Config, name: str) -> str:
    """The library path to store for `pin add NAME`: a movie, a show or one season, on any disk."""
    rel = resolve_title(cfg.ssd_branches + cfg.hdd_branches, cfg, name, "on any disk")
    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    if core.unit_of(rel + "/x.mkv", cfg.series_roots, cfg.movie_roots) is None and parent not in cfg.series_roots:
        raise ValueError(f"{name!r}: {rel} is not a movie, a show or a show-season folder")
    return rel


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
    return _locked(cfg, now(), dry_run, lambda: _run_move(cfg, jf, names, dry_run, wait, sleep, poll, now))


def _run_move(
    cfg: Config,
    jf: Jellyfin,
    names: list[str],
    dry_run: bool,
    wait: bool,
    sleep: Callable[[float], object],
    poll: float,
    now: Callable[[], float],
) -> dict[str, Any]:
    started = now()
    result = _new_result(started)
    units: dict[str, core.Unit] = {}
    bad = statemod.problem(cfg.state_path)
    if bad:
        result.update(finished=now(), failed=True, note=f"the state file can't be read ({bad}): not moving anything")
        return result
    if stand := _stand_down(cfg, started):
        result.update(finished=now(), note=stand[0])
        if stand[1]:
            result["failed"] = True
        return result
    state = statemod.load(cfg.state_path)

    def finish(note: str, failed: bool = False) -> dict[str, Any]:
        result["note"], result["finished"] = note, now()
        if failed:
            result["failed"] = True
        if units:
            _publish_tiers(cfg, units, now())
        _hook(cfg, result, dry_run)
        if not dry_run:
            state["last_run"] = result
            statemod.save(cfg.state_path, state)
        return result

    if why := _precheck(cfg):
        return finish(why, failed=True)
    wanted = resolve_units(cfg, names)
    titles = pins.load(cfg.pin_file)  # not damaged: _stand_down checked
    units_to_move = []
    for rel in wanted:
        if pins.is_pinned(rel, titles, cfg.roots):
            result["skipped"].append({"unit": rel, "why": "pinned: never moved off the SSD"})
        else:
            units_to_move.append(rel)
    if dry_run:
        result["would_move_to_hdd"] = units_to_move
        return finish("dry run: nothing was moved")
    if not units_to_move:
        return finish("nothing to move: every named title is pinned")
    quiet_deadline = started + cfg.wait_quiet_minutes * 60
    while wait and watch.current_playing(jf, cfg.path_prefix):
        if now() > quiet_deadline:
            return finish("people were watching the whole time: nothing moved")
        sleep(60)
    # Even with --no-wait, a title that is playing right now is never moved.
    playing_now = {
        found[0]
        for path in watch.current_playing(jf, cfg.path_prefix)
        if (found := core.unit_of(path, cfg.series_roots, cfg.movie_roots))
    }
    for rel in [r for r in units_to_move if r in playing_now]:
        units_to_move.remove(rel)
        result["skipped"].append({"unit": rel, "why": "playing now"})
    watcher = PlayingWatcher(cfg, jf, poll)
    watcher.start()
    not_mounted = ""
    try:
        for rel in units_to_move:
            if watcher.flag:
                result["skipped"].append({"unit": rel, "why": watcher.reason})
                continue
            try:
                r = disks.move_unit(
                    rel,
                    cfg.ssd_branches,
                    cfg.hdd_branches,
                    lambda: watcher.flag,
                    None,
                    cfg.dest_margin,
                    marker=cfg.disk_marker if cfg.check_mounts else None,
                    reserves=dict(cfg.branch_reserve),
                )
            except OSError as exc:
                log.warning("moving %s failed: %s", rel, exc)
                result["skipped"].append({"unit": rel, "why": f"{type(exc).__name__}: {exc}"[:200]})
                continue
            if r["moved"]:
                result["demoted"].append({"unit": rel, "bytes": r["bytes"]})
            if r["moved"] and not r["skipped"]:
                state.setdefault("cooldown", {})[rel] = {"dir": "demote", "at": now()}
            if r["skipped"]:
                result["skipped"].append({"unit": rel, "why": r["skipped"]})
                if r["skipped"].startswith(disks.NOT_MOUNTED):
                    log.error("ALERT: %s. Stopping.", r["skipped"])
                    not_mounted = r["skipped"]
                    break
    except Exception as exc:
        log.exception("manual move failed")
        return finish(
            f"stopped by an error ({type(exc).__name__}: {exc}); progress so far was saved"[:300], failed=True
        )
    finally:
        watcher.stop()
        try:
            # Walk the HDDs again after a move so the saved file list matches; also gives tiers.json its data.
            after = disks.scan_files(
                cfg.ssd_branches,
                cfg.hdd_branches,
                cfg.roots,
                cfg.hdd_index_path,
                cfg.hdd_index_max_days,
                fresh_hdd=bool(result["demoted"]),
                marker=cfg.disk_marker,
            )
            units = core.units_from_files(after, cfg.series_roots, cfg.movie_roots)
        except Exception as exc:  # noqa: BLE001
            log.warning("file list not refreshed after the move: %s", exc)
    if not_mounted:
        return finish(f"{not_mounted}. Stopped; nothing else was moved.", failed=True)
    return finish("manual move: ok")


# ---------------------------------------------------------- reporting
def summary(result: dict[str, Any]) -> str:
    gb: Callable[[list[dict[str, Any]]], float] = lambda items: sum(i["bytes"] for i in items) / core.GB  # noqa: E731
    lines = [f"LomboTier: {result['note']}"]
    if result["demoted"]:
        lines.append(f"Moved to HDD: {len(result['demoted'])} title(s), {gb(result['demoted']):.1f} GB")
    if result["promoted"]:
        lines.append(f"Moved back to SSD: {len(result['promoted'])} title(s), {gb(result['promoted']):.1f} GB")
    if result.get("gathered"):
        lines.append(f"Gathered pinned titles: {len(result['gathered'])} title(s), {gb(result['gathered']):.1f} GB")
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
    moved = result["demoted"] or result["promoted"] or result.get("gathered")
    if cfg.webhook_url and (failed or moved):
        try:
            notify.send(cfg.webhook_url, summary(result))
        except notify.NotifyError as exc:
            log.warning("%s", exc)
