"""Command line. Talks to the outside world; core.py decides.

lombo-tier doctor               check the settings, Jellyfin, the disks and the paths; exit 1 on any failure
lombo-tier plan [--json]        show what a run would move; changes nothing
lombo-tier run [--no-wait] [--loop]
                                do it (only prints the plan while DRY_RUN=true); --loop repeats every day at RUN_HOUR
lombo-tier move NAME [NAME...] [--dry-run]
                                move the named titles to the HDDs now, with the same safety checks
lombo-tier pin add|remove NAME [NAME...]   keep a title on the SSD (the pin list, LOMBO_TIER_PIN_FILE)
lombo-tier pin list             show the pinned titles
"""

import argparse
import contextlib
import logging
import os
import subprocess
import sys
import time
from typing import Any

from . import __version__, core, disks, mover, pins, schedule, session
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
                line(
                    False, f"{tier} {b}", "not writable by this user (a read-only mount, or wrong owner or permissions)"
                )
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
    if cfg.pin_file:
        pin_bad = pins.problem(cfg.pin_file)
        count = 0 if pin_bad else len(pins.load(cfg.pin_file))
        line(pin_bad is None, f"pin list {cfg.pin_file}", pin_bad or f"{count} pinned title(s)")
        if cfg.pin_branch:
            if cfg.check_mounts:
                here = disks.disk_mounted(cfg.pin_branch, cfg.disk_marker)
            else:
                here = os.path.isdir(cfg.pin_branch)
            why = "" if here else "missing or not mounted: pinned titles will not move"
            line(here, f"pin branch {cfg.pin_branch}", why)
    if cfg.session_file:
        status, detail = session.check(cfg.session_file, time.time())
        if status == session.UNREADABLE:
            line(False, f"conversion session file {cfg.session_file}", f"{detail}: runs will stand down")
        elif status == session.OPEN:
            print(f"[warn] a conversion session is open ({detail}): runs will not move anything until it closes")
        else:
            line(True, f"conversion session file {cfg.session_file}", "no session open")
    print(f"[ ok ] webhook: {'set' if cfg.webhook_url else 'not set (no alerts)'}")
    print(f"[ ok ] mode: {'dry run (nothing is moved)' if cfg.dry_run else 'LIVE'}")
    return 1 if bad else 0


# -------------------------------------------------------------------- plan
def format_plan(p: core.Plan, free: int, cfg: Config) -> str:
    s = cfg.settings
    out = [f"SSD free (counted for planning): {_gb(free)}; target {_gb(s.reserve + s.headroom)}"]
    for title, moves in (
        ("Move to HDD", p.demote),
        ("Move back to SSD", p.promote),
        ("Gather pinned titles onto the pin branch", p.gather),
    ):
        if title.startswith("Gather") and not cfg.pin_branch:
            continue
        out.append(f"{title} ({len(moves)}):")
        out += [f"  {m.rel}  {_gb(m.bytes)}  {m.why}" for m in moves] or ["  nothing"]
    if p.popular:
        out.append(
            "Most watched recently: "
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
    for title, key in (
        ("Would move to HDD", "demote"),
        ("Would move back to SSD", "promote"),
        ("Would gather pinned titles", "gather"),
    ):
        if key == "gather" and not plan[key]:
            continue
        lines.append(f"{title} ({len(plan[key])}):")
        lines += [f"  {m['rel']}  {_gb(m['bytes'])}  {m['why']}" for m in plan[key]] or ["  nothing"]
    lines += [f"Note: {n}" for n in plan["notes"]]
    lines.append("Set LOMBO_TIER_DRY_RUN=false to let it move files.")
    return "\n".join(lines)


def cmd_move(cfg: Config, jf: Jellyfin, names: list[str], wait: bool, dry_run: bool = False) -> int:
    dry = dry_run or cfg.dry_run  # the flag can only make it safer
    try:
        result = mover.run_move(cfg, jf, names, dry_run=dry, wait=wait)
    except ValueError as e:  # a mistyped or ambiguous name: nothing moved, not a failed run
        print(f"lombo-tier: {e}", file=sys.stderr)
        return 2
    if result.get("busy"):
        print(f"lombo-tier: {result['note']}", file=sys.stderr)
        return 1
    if dry and "would_move_to_hdd" in result:
        would = ", ".join(result["would_move_to_hdd"]) or "nothing"
        out = [f"LomboTier (dry run, nothing moved). Would move to HDD: {would}"]
        out += [f"Skipped {s['unit']}: {s['why']}" for s in result["skipped"]]
        print("\n".join(out))
    elif dry:
        print(f"LomboTier (dry run, nothing moved). {result['note']}")
    else:
        print(mover.summary(result))
    return 1 if result.get("failed") else 0


# --------------------------------------------------------------------- pin
def cmd_pin(cfg: Config, action: str, names: list[str]) -> int:
    """Edit or show the pin list. It only changes the list file, never a media file, so DRY_RUN does not apply."""
    if not cfg.pin_file:
        print("lombo-tier: LOMBO_TIER_PIN_FILE is not set: say where the pin list lives first", file=sys.stderr)
        return 2
    if bad := pins.problem(cfg.pin_file):
        print(f"lombo-tier: the pin list {cfg.pin_file} can't be read ({bad}); fix it first", file=sys.stderr)
        return 1
    titles = pins.load(cfg.pin_file)
    if action == "list":
        print("\n".join(titles) if titles else "no pinned titles")
        return 0
    new = list(titles)
    try:
        for name in names:
            if action == "add":
                new.append(mover.resolve_pin(cfg, name))
            else:
                name = name.strip().strip("/")
                hit = sorted({t for t in new if t == name or any(t == f"{r}/{name}" for r in cfg.roots)})
                if len(hit) != 1:
                    have = ", ".join(sorted(new)) or "nothing"
                    why = f"not in the pin list (pinned: {have})" if not hit else "matches several: " + ", ".join(hit)
                    raise ValueError(f"{name!r}: {why}")
                new.remove(hit[0])
    except ValueError as e:
        print(f"lombo-tier: {e}", file=sys.stderr)
        return 2
    try:
        pins.save(cfg.pin_file, new)
    except OSError as e:
        print(f"lombo-tier: can't write the pin list {cfg.pin_file} ({type(e).__name__})", file=sys.stderr)
        return 1
    print(f"{len(set(new))} pinned title(s)")
    return 0


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
    mv.add_argument("--dry-run", action="store_true", help="print what would move and change nothing")
    pn = sub.add_parser("pin")
    pn.add_argument("action", choices=["add", "remove", "list"])
    pn.add_argument("names", nargs="*", metavar="NAME")
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
    if args.cmd == "pin":
        if args.action != "list" and not args.names:
            p.error(f"pin {args.action} needs at least one NAME")
        return cmd_pin(cfg, args.action, args.names)
    try:
        if args.cmd == "plan":
            return cmd_plan(cfg, jf, args.json)
        _low_priority()
        if args.cmd == "run":
            return cmd_run(cfg, jf, not args.no_wait, args.loop)
        return cmd_move(cfg, jf, args.names, not args.no_wait, args.dry_run)
    except JellyfinError as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
