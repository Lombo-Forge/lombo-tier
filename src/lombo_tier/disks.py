"""The disk layer: scanning branches and moving folders between them safely.

A move copies each file to a temporary name on the destination, hashes it while copying, flushes it to
disk, reads it back and compares the hash, and only then renames it into place. The source file is
deleted only after that verified copy is in place, so a crash or a stop at any moment loses nothing.
"""

import contextlib
import hashlib
import json
import logging
import os
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .core import DAY, GB, Entry

log = logging.getLogger("lombotier.disks")

PART_SUFFIX = ".lombotier-part"
NOT_MOUNTED = "disk not mounted"
CHUNK = 8 * 1024 * 1024


class Skip(Exception):
    """This file (or unit) cannot be moved right now; the reason is the message."""


class Stopped(Exception):
    """The mover was told to stop mid-file (someone started watching, or the time is up)."""


# ------------------------------------------------------------------ disks
def disk_free(path: str) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def disk_mounted(branch: str, marker: str = "") -> bool:
    """A pool disk is there when its marker folder exists; with no marker, when the path is a mount point.

    An empty mount-point folder left on the boot disk (the disk did not mount) has neither."""
    if marker:
        return os.path.isdir(os.path.join(branch, marker))
    return os.path.ismount(branch)


def unmounted_branches(branches: Sequence[str], marker: str = "") -> list[str]:
    return [b for b in branches if not disk_mounted(b, marker)]


def branch_free(branches: Sequence[str], less: Mapping[str, int] | None = None) -> list[tuple[int, int]]:
    """[(free_bytes, filesystem id)] for each readable branch, for core.effective_free. `less` is taken off a
    branch's free space: how much more than the usual reserve that branch keeps."""
    out: list[tuple[int, int]] = []
    for b in branches:
        try:
            out.append((max(0, disk_free(b) - (less or {}).get(b, 0)), os.stat(b).st_dev))
        except OSError:
            continue
    return out


def pick_branch(
    branches: Sequence[str], need: int, margin: int = 20 * GB, reserves: Mapping[str, int] | None = None
) -> str | None:
    """The branch with the most free space that can take `need` bytes and still keep `margin` free, or that
    branch's own reserve when that is more."""
    best, best_free = None, -1
    for b in branches:
        try:
            free = disk_free(b)
        except OSError:
            continue
        if free - need >= max(margin, (reserves or {}).get(b, 0)) and free > best_free:
            best, best_free = b, free
    return best


# ------------------------------------------------------------------- scan
def _walk_branch(branch: str, roots: Sequence[str]) -> list[tuple[str, int, int, float]]:
    out: list[tuple[str, int, int, float]] = []
    for root in roots:
        for dirpath, _dirs, names in os.walk(os.path.join(branch, root)):
            rel_dir = os.path.relpath(dirpath, branch).replace(os.sep, "/")
            for name in names:
                if name.endswith(PART_SUFFIX):
                    continue
                try:
                    st = os.stat(os.path.join(dirpath, name))
                except OSError:
                    continue
                out.append((f"{rel_dir}/{name}", st.st_size, st.st_nlink, st.st_mtime))
    return out


def _load_hdd_index(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_hdd_index(path: str | None, index: dict[str, Any]) -> None:
    if not path:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(index, f)
        os.replace(tmp, path)
    except OSError as exc:  # read-only state folder: the cache just is not kept
        log.debug("HDD index not saved: %s", exc)


def scan_files(
    ssd_branches: Sequence[str],
    hdd_branches: Sequence[str],
    roots: Sequence[str],
    hdd_index_path: str | None = None,
    hdd_max_age_days: float = 7,
    fresh_hdd: bool = False,
    now: float | None = None,
    marker: str = "",
) -> dict[str, Entry]:
    """One walk of every branch's library roots: {pool-relative path: Entry}.

    Branches are separate disks, so they are walked in parallel threads. HDDs are walked as little as
    possible so they can stay asleep: with `hdd_index_path` set, an HDD's file list comes from the saved
    index, and the disk is only touched when its entry is missing, older than `hdd_max_age_days`, or
    `fresh_hdd` is set. SSDs are always walked."""
    now = time.time() if now is None else now
    index = _load_hdd_index(hdd_index_path) if hdd_index_path else {}
    to_walk = [(b, True) for b in ssd_branches]
    cached: dict[str, list[tuple[str, int, int, float]]] = {}
    for b in hdd_branches:
        entry = index.get(b)
        if hdd_index_path and not fresh_hdd and entry and now - entry.get("at", 0) < hdd_max_age_days * DAY:
            cached[b] = [tuple(r) for r in entry["rows"]]
        else:
            to_walk.append((b, False))
    branches = to_walk
    files: dict[str, Entry] = {}
    if not branches and not cached:
        return files
    walked: list[list[tuple[str, int, int, float]]] = []
    if branches:
        with ThreadPoolExecutor(max_workers=min(8, len(branches))) as pool:
            walked = list(pool.map(lambda bt: _walk_branch(bt[0], roots), branches))
    if hdd_index_path:
        new_index = {b: e for b, e in index.items() if b in hdd_branches}  # forget removed disks
        for (b, is_ssd), rows in zip(branches, walked, strict=True):
            # An unmounted HDD is an empty folder on the boot disk: never cache it as empty.
            if not is_ssd and disk_mounted(b, marker):
                new_index[b] = {"at": now, "rows": rows}
            elif not is_ssd:
                new_index.pop(b, None)
        if any(not s for _b, s in branches):
            _save_hdd_index(hdd_index_path, new_index)
    branches = branches + [(b, False) for b in cached]
    walked = walked + list(cached.values())
    for (_branch, is_ssd), rows in zip(branches, walked, strict=True):
        for rel, size, nlink, mtime in rows:
            e = files.get(rel)
            if e is None:
                files[rel] = Entry(size, is_ssd, not is_ssd, nlink, mtime)
            else:  # on both tiers (a move in progress): count it once, remember both
                e.ssd, e.hdd = e.ssd or is_ssd, e.hdd or not is_ssd
                e.nlink = max(e.nlink, nlink)
    return files


# ------------------------------------------------------------------ moving
def _advise(fd: int, kind: str) -> None:
    """Tell the kernel how a big sequential file is used (Linux). Best effort."""
    flag = getattr(os, "POSIX_FADV_SEQUENTIAL" if kind == "seq" else "POSIX_FADV_DONTNEED", None)
    if flag is not None:
        with contextlib.suppress(OSError):
            os.posix_fadvise(fd, 0, 0, flag)


def _sha(path: str, stop: Callable[[], str | None] | None = None) -> str:
    """SHA-256 of a file without filling the page cache with it. `stop` is checked between chunks
    (not before the first)."""
    with open(path, "rb") as f:
        _advise(f.fileno(), "seq")
        if stop is None:
            digest = hashlib.file_digest(f, "sha256").hexdigest()
        else:
            h = hashlib.sha256()
            first = True
            while chunk := f.read(CHUNK):
                if not first and (reason := stop()):
                    raise Stopped(reason)
                first = False
                h.update(chunk)
            digest = h.hexdigest()
        _advise(f.fileno(), "drop")
    return digest


def copy_verified(
    src: str,
    dst: str,
    now: float | None = None,
    stop: Callable[[], str | None] | None = None,
    settle_seconds: float = 3600,
) -> None:
    """Copy src to dst (temp name, hash while copying, fsync, read back, rename).

    Refuses a file that is hard-linked (a torrent is seeding it) or changed within `settle_seconds`.
    `stop` is an optional callable returning a reason (truthy) when the copy must end now. It is
    checked between chunks, not before the first one, and raises Stopped: the temp file is removed and
    the original and destination are untouched."""
    st = os.stat(src)
    if st.st_nlink > 1:
        raise Skip("still hard-linked (a torrent is seeding it)")
    if (now or time.time()) - st.st_mtime < settle_seconds:
        raise Skip("changed recently")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        if os.path.getsize(dst) == st.st_size and _sha(dst) == _sha(src):
            return  # an earlier, interrupted run already copied it
        raise Skip("a different file is already at the destination")
    part = os.path.join(os.path.dirname(dst), "." + os.path.basename(dst) + PART_SUFFIX)
    h = hashlib.sha256()
    buf = bytearray(CHUNK)
    view = memoryview(buf)
    try:
        with open(src, "rb", buffering=0) as fi, open(part, "wb", buffering=0) as fo:
            _advise(fi.fileno(), "seq")
            first = True
            while n := fi.readinto(buf):
                if stop is not None and not first and (reason := stop()):
                    raise Stopped(reason)
                first = False
                h.update(view[:n])
                fo.write(view[:n])
            os.fsync(fo.fileno())
            _advise(fi.fileno(), "drop")
            _advise(fo.fileno(), "drop")
        shutil.copystat(src, part)
        if os.path.getsize(part) != st.st_size or _sha(part, stop) != h.hexdigest():
            raise Skip("the copy did not match the original")
        os.replace(part, dst)
    finally:
        if os.path.exists(part):
            os.unlink(part)


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_stale_copies(good: str, rel: str, branches: Sequence[str]) -> None:
    """Delete copies of `rel` on other branches, only if byte-identical to `good` (a verified, kept copy)."""
    for b in branches:
        other = os.path.join(b, rel)
        if other == good or not os.path.isfile(other):
            continue
        with contextlib.suppress(OSError):
            if os.path.getsize(other) == os.path.getsize(good) and _sha(other) == _sha(good):
                os.unlink(other)


def move_unit(
    unit_rel: str,
    src_branches: Sequence[str],
    dst_branches: Sequence[str],
    check_playing: Callable[[], bool] = lambda: False,
    deadline: float | None = None,
    margin: int = 20 * GB,
    settle_seconds: float = 3600,
    marker: str | None = None,
    reserves: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Move one unit's files to the best destination branch. Returns what happened.

    With `marker` set (the disk marker, "" for mount-point checks), every branch is re-checked as
    mounted before each file; if one is gone the unit stops and `skipped` starts with `NOT_MOUNTED`.
    A file that cannot move for a known reason (hard-linked, changed recently) is found before the
    first copy, so the unit is left whole. A stop mid-unit (someone watching, time up) can still leave
    it partly moved; the next run carries on from there."""
    files = []
    for b in src_branches:
        for dirpath, _d, names in os.walk(os.path.join(b, unit_rel)):
            for n in names:
                if not n.endswith(PART_SUFFIX):
                    files.append((b, os.path.join(dirpath, n)))
    total = sum(os.path.getsize(p) for _b, p in files)
    if not files:
        return {"moved": 0, "bytes": 0, "skipped": "nothing to move"}
    for _b, path in files:
        st = os.stat(path)
        if st.st_nlink > 1:
            why = "still hard-linked (a torrent is seeding it)"
        elif time.time() - st.st_mtime < settle_seconds:
            why = "changed recently"
        else:
            continue
        return {"moved": 0, "bytes": 0, "skipped": f"{os.path.basename(path)}: {why}"}

    def gone() -> str | None:
        if marker is None:
            return None
        missing = unmounted_branches([*src_branches, *dst_branches], marker)
        return f"{NOT_MOUNTED}: {', '.join(missing)}" if missing else None

    if reason := gone():
        return {"moved": 0, "bytes": 0, "skipped": reason}
    dst_branch = pick_branch(dst_branches, total, margin, reserves)
    if dst_branch is None:
        return {"moved": 0, "bytes": 0, "skipped": "no room on the destination disks"}
    moved = moved_bytes = 0

    def stop_reason() -> str | None:
        if deadline and time.time() > deadline:
            return "out of time (continues next time)"
        if check_playing():
            return "someone started watching"
        return None

    for b, path in files:
        if reason := stop_reason() or gone():
            return {"moved": moved, "bytes": moved_bytes, "skipped": reason}
        rel = os.path.relpath(path, b)
        size = os.path.getsize(path)
        # An aborted earlier run may have left a copy on another destination disk: reuse that one
        # (copy_verified hash-checks it) so the file is not stored twice.
        target = next((db for db in dst_branches if os.path.isfile(os.path.join(db, rel))), dst_branch)
        dst = os.path.join(target, rel)
        try:
            copy_verified(path, dst, stop=stop_reason, settle_seconds=settle_seconds)
        except Stopped as exc:
            return {"moved": moved, "bytes": moved_bytes, "skipped": str(exc)}
        except Skip as exc:
            return {"moved": moved, "bytes": moved_bytes, "skipped": f"{os.path.basename(path)}: {exc}"}
        _fsync_dir(os.path.dirname(dst))  # the rename must be on disk before the only other copy goes
        _remove_stale_copies(dst, rel, dst_branches)
        os.unlink(path)  # the verified copy is in place; the pool now serves it from there
        moved += 1
        moved_bytes += size
    for b in src_branches:  # tidy: remove directories that are now empty
        for dirpath, _d, _n in os.walk(os.path.join(b, unit_rel), topdown=False):
            with contextlib.suppress(OSError):
                os.rmdir(dirpath)
    return {"moved": moved, "bytes": moved_bytes, "skipped": None, "to": dst_branch}
