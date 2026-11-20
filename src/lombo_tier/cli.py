"""Command line. Talks to the outside world; core.py decides.

lombo-tier doctor               check the settings, Jellyfin, the disks and the paths; exit 1 on any failure
lombo-tier plan [--json]        show what a run would move; changes nothing
lombo-tier run [--no-wait] [--loop]
                                do it (only prints the plan while DRY_RUN=true); --loop repeats every day at RUN_HOUR
lombo-tier move NAME [NAME...]  move the named titles to the HDDs now, with the same safety checks
"""

import argparse
import contextlib
import logging
import os
import subprocess
import sys
import time
from typing import Any

from . import __version__, core, disks, mover, schedule
from . import state as statemod
from .config import Config, ConfigError, from_env
from .jellyfin import Jellyfin, JellyfinError

GB = core.GB


def _gb(n: int) -> str:
    return f"{n / GB:.1f} GB"


# ------------------------------------------------------------------ doctor
def doctor(cfg: Config, jf: Jellyfin | None = None) -> int:
    jf = jf or Jellyfin(cfg.jellyfin_url, cfg.jellyfin_api_key)
    bad = 0

    def line(ok: bool, label: str, detail: str = "") -> None:
        nonlocal bad
        bad += not ok
        print(
            f"[ ok ] {label}" + (f": {detail}" if detail else "")
            if ok
            else f"[FAIL] {label}" + (f": {detail}" if detail else "")
        )

    try:
        info = jf.public_info()
        line(True, "Jellyfin reachable", f"{info.get('ServerName')} {info.get('Version')}")
    except JellyfinError as e:
        line(False, "Jellyfin reachable", str(e))
    try:
        jf.info()
        line(True, "API key accepted")
        sample = [
            i.get("Path")
            for i in jf._get(
                "/Items", {"Recursive": "true", "IncludeItemTypes": "Movie,Episode", "Fields": "Path", "Limit": "20"}
            ).get("Items", [])
            if i.get("Path")
        ]
        inside = [p for p in sample if core.rel_from_media_path(p, cfg.path_prefix) is not None]
        if not sample:
            print("[warn] path prefix: Jellyfin has no movies or episodes yet, so it cannot be checked")
        elif inside:
            line(True, "path prefix", f"Jellyfin's paths start with {cfg.path_prefix}")
        else:
            hint = core.suggest_prefix(sample[0], cfg.roots)
            line(
                False,
                "path prefix",
                f"Jellyfin reports {sample[0]!r}, which does not start with "
                f"{cfg.path_prefix!r}" + (f". Try LOMBO_TIER_JELLYFIN_PATH_PREFIX={hint}" if hint else ""),
            )
    except JellyfinError as e:
        line(False, "API key accepted", str(e))

    for tier, branches in (("SSD", cfg.ssd_branches), ("HDD", cfg.hdd_branches)):
        for b in branches:
            if not os.path.isdir(b):
                line(False, f"{tier} {b}", "folder does not exist (is it mounted into the container at this path?)")
            elif cfg.check_mounts and not disks.disk_mounted(b, cfg.disk_marker):
                line(False, f"{tier} {b}", "not mounted (an empty folder on the boot disk would fill it)")
            elif not os.access(b, os.W_OK):
                line(False, f"{tier} {b}", "not writable by this user")
            else:
                line(True, f"{tier} {b}", f"{_gb(disks.disk_free(b))} free")
    present = {}
    for root in cfg.roots:
        present[root] = [b for b in cfg.ssd_branches + cfg.hdd_branches if os.path.isdir(os.path.join(b, root))]
    for root, found in present.items():
        if found:
            line(True, f"library folder {root}", f"on {len(found)} disk(s)")
        elif any(present.values()):
            # Fine for a library that has no TV (or no movies); the other folders do exist.
            print(f"[warn] library folder {root}: not found on any disk (nothing there will be moved)")
    if not any(present.values()):
        line(
            False,
            "library folders",
            f"none of {', '.join(cfg.roots)} exist on any disk. "
            "Check LOMBO_TIER_MOVIE_ROOTS and LOMBO_TIER_SERIES_ROOTS",
        )

    perr = statemod.problem(cfg.state_path)
    try:
        os.makedirs(cfg.data_dir, exist_ok=True)
        writable = os.access(cfg.data_dir, os.W_OK)
    except OSError:
        writable = False
    line(writable, f"data folder {cfg.data_dir}", "" if writable else "not writable")
    line(perr is None, "state file", perr or "readable or not created yet")
    print(f"[ ok ] webhook: {'set' if cfg.webhook_url else 'not set (no alerts)'}")
    print(f"[ ok ] mode: {'dry run (nothing is moved)' if cfg.dry_run else 'LIVE'}")
    return 1 if bad else 0


# -------------------------------------------------------------------- plan
def format_plan(p: core.Plan, free: int, cfg: Config) -> str:
    s = cfg.settings
    out = [f"SSD free (counted for planning): {_gb(free)}; target {_gb(s.reserve + s.headroom)}"]
    for title, moves in (("Move to HDD", p.demote), ("Move back to SSD", p.promote)):
        out.append(f"{title} ({len(moves)}):")
        out += [f"  {m.rel}  {_gb(m.bytes)}  {m.why}" for m in moves] or ["  nothing"]
    if p.popular:
        out.append(
            "Popular right now: "
            + ", ".join(f"{x['unit']} ({x['viewers']} viewers, on {x['where']})" for x in p.popular[:5])
        )
    out += [f"Note: {n}" for n in p.notes]
    return "\n".join(out)


def cmd_plan(cfg: Config, jf: Jellyfin, as_json: bool) -> int:
    import json

    p, _units, free = mover.make_plan(cfg, jf)
    print(json.dumps(mover.plan_json(p), indent=1) if as_json else format_plan(p, free, cfg))
    return 0


# --------------------------------------------------------------------- run
def cmd_run(cfg: Config, jf: Jellyfin, wait: bool, loop: bool) -> int:
    while True:
        if loop:
            secs = schedule.seconds_until(cfg.run_hour, time.time(), os.environ.get("TZ", "UTC"))
            logging.info("next run in %d h %02d min", secs // 3600, secs % 3600 // 60)
            time.sleep(secs)
        try:
            result = mover.run(cfg, jf, wait=wait)
        except Exception as exc:
            result = {
                "failed": True,
                "note": f"{type(exc).__name__}: {exc}",
                "demoted": [],
                "promoted": [],
                "skipped": [],
            }
            logging.exception("run failed")
        if not cfg.dry_run:
            mover.report(cfg, result)
        print(mover.summary(result) if not cfg.dry_run else _dry_summary(result, cfg))
        if not loop:
            return 1 if result.get("failed") else 0


def _dry_summary(result: dict[str, Any], cfg: Config) -> str:
    plan = result.get("plan")
    if not plan:
        return f"LomboTier: {result['note']}"
    lines = ["LomboTier (dry run, nothing moved). Tonight's plan:"]
    for title, key in (("Would move to HDD", "demote"), ("Would move back to SSD", "promote")):
        lines.append(f"{title} ({len(plan[key])}):")
        lines += [f"  {m['rel']}  {_gb(m['bytes'])}  {m['why']}" for m in plan[key]] or ["  nothing"]
    lines += [f"Note: {n}" for n in plan["notes"]]
    lines.append("Set LOMBO_TIER_DRY_RUN=false to let it move files.")
    return "\n".join(lines)


def cmd_move(cfg: Config, jf: Jellyfin, names: list[str], wait: bool) -> int:
    try:
        result = mover.run_move(cfg, jf, names, wait=wait)
    except ValueError as e:  # a mistyped name: not a failed run
        print(f"lombo-tier: {e}", file=sys.stderr)
        return 2
    if cfg.dry_run:
        would = ", ".join(result.get("would_move_to_hdd", [])) or result["note"]
        print(f"LomboTier (dry run, nothing moved). Would move to HDD: {would}")
    else:
        print(mover.summary(result))
    return 1 if result.get("failed") else 0


# -------------------------------------------------------------------- main
def _low_priority() -> None:
    """Never slow a viewer down: lowest CPU and idle disk priority, best effort."""
    with contextlib.suppress(OSError):  # FileNotFoundError (no ionice) is an OSError
        os.nice(10)
        cmd = ["ionice", "-c3", "-p", str(os.getpid())]
        subprocess.run(cmd, capture_output=True, check=False)  # noqa: S603 - fixed command


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lombo-tier", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    pl = sub.add_parser("plan")
    pl.add_argument("--json", action="store_true")
    run = sub.add_parser("run")
    run.add_argument("--no-wait", action="store_true", help="do not wait for people to stop watching")
    run.add_argument("--loop", action="store_true")
    mv = sub.add_parser("move")
    mv.add_argument("names", nargs="+", metavar="NAME")
    mv.add_argument("--no-wait", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        cfg = from_env(dict(os.environ) if env is None else env)
    except ConfigError as e:
        print(f"[FAIL] settings: {e}", file=sys.stderr)
        return 2
    jf = Jellyfin(cfg.jellyfin_url, cfg.jellyfin_api_key)
    if args.cmd == "doctor":
        return doctor(cfg, jf)
    try:
        if args.cmd == "plan":
            return cmd_plan(cfg, jf, args.json)
        _low_priority()
        if args.cmd == "run":
            return cmd_run(cfg, jf, not args.no_wait, args.loop)
        return cmd_move(cfg, jf, args.names, not args.no_wait)
    except JellyfinError as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
