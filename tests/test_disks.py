import os
import pathlib
import time

import pytest

from lombo_tier import core, disks

OLD = 10 * 86400


def make(path, data=b"x" * 100, age=OLD):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    t = time.time() - age
    os.utime(path, (t, t))
    return path


@pytest.fixture
def pool(tmp_path):
    for d in ("ssd1", "ssd2", "hdd1", "hdd2"):
        (tmp_path / d / ".pool").mkdir(parents=True)
    return tmp_path


# ------------------------------------------------------------------ scan
def test_scan_merges_both_tiers_and_sums_units(pool):
    make(pool / "ssd1/movies/A (2020)/a.mkv", b"x" * 100)
    make(pool / "hdd1/movies/A (2020)/a.mkv", b"x" * 100)  # same file on both tiers
    make(pool / "hdd1/movies/A (2020)/a.srt", b"y" * 10)
    make(pool / "ssd2/library/tv/Show/Season 1/e1.mkv", b"z" * 50)
    make(pool / "ssd1/other/ignored.mkv")
    make(pool / "ssd1/movies/A (2020)" / ("." + "a.mkv" + disks.PART_SUFFIX))
    files = disks.scan_files([str(pool / "ssd1"), str(pool / "ssd2")], [str(pool / "hdd1")], ["movies", "library/tv"])
    assert set(files) == {"movies/A (2020)/a.mkv", "movies/A (2020)/a.srt", "library/tv/Show/Season 1/e1.mkv"}
    e = files["movies/A (2020)/a.mkv"]
    assert e.ssd
    assert e.hdd
    assert e.size == 100
    units = core.units_from_files(files, ["library/tv"], ["movies"])
    assert units["movies/A (2020)"].ssd_bytes == 100
    assert units["movies/A (2020)"].hdd_bytes == 10
    assert units["library/tv/Show/Season 1"].ssd_bytes == 50


def _counting(monkeypatch):
    walked = []
    real = disks._walk_branch

    def spy(branch, roots):
        walked.append(branch)
        return real(branch, roots)

    monkeypatch.setattr(disks, "_walk_branch", spy)
    return walked


def test_hdd_index_not_rewalked_while_fresh(pool, monkeypatch):
    make(pool / "hdd1/movies/A/a.mkv")
    idx = str(pool / "idx.json")
    hdd = [str(pool / "hdd1")]
    walked = _counting(monkeypatch)
    f1 = disks.scan_files([], hdd, ["movies"], idx, now=1000.0, marker=".pool")
    f2 = disks.scan_files([], hdd, ["movies"], idx, now=1000.0 + DAYS(1), marker=".pool")
    assert walked == hdd
    assert f1.keys() == f2.keys() == {"movies/A/a.mkv"}


def DAYS(n):
    return n * 86400


def test_hdd_index_rewalked_when_stale_or_fresh(pool, monkeypatch):
    make(pool / "hdd1/movies/A/a.mkv")
    idx = str(pool / "idx.json")
    hdd = [str(pool / "hdd1")]
    walked = _counting(monkeypatch)
    disks.scan_files([], hdd, ["movies"], idx, now=1000.0, marker=".pool")
    disks.scan_files([], hdd, ["movies"], idx, now=1000.0 + DAYS(8), marker=".pool")
    assert len(walked) == 2
    disks.scan_files([], hdd, ["movies"], idx, now=1000.0 + DAYS(8), marker=".pool", fresh_hdd=True)
    assert len(walked) == 3


def test_unmounted_hdd_not_cached_as_empty(pool, monkeypatch):
    hdd = pool / "hdd3"
    hdd.mkdir()  # no marker folder: not mounted
    idx = str(pool / "idx.json")
    walked = _counting(monkeypatch)
    disks.scan_files([], [str(hdd)], ["movies"], idx, now=1000.0, marker=".pool")
    disks.scan_files([], [str(hdd)], ["movies"], idx, now=1001.0, marker=".pool")
    assert len(walked) == 2  # walked again: nothing was cached
    (hdd / ".pool").mkdir()
    make(hdd / "movies/B/b.mkv")
    assert "movies/B/b.mkv" in disks.scan_files([], [str(hdd)], ["movies"], idx, now=1002.0, marker=".pool")


def test_index_reflects_move_after_fresh_walk(pool):
    src = make(pool / "hdd1/movies/A/a.mkv")
    idx = str(pool / "idx.json")
    ssd, hdd = [str(pool / "ssd1")], [str(pool / "hdd1")]
    assert disks.scan_files(ssd, hdd, ["movies"], idx, now=1000.0, marker=".pool")["movies/A/a.mkv"].hdd
    make(pool / "ssd1/movies/A/a.mkv")
    src.unlink()
    stale = disks.scan_files(ssd, hdd, ["movies"], idx, now=1001.0, marker=".pool")["movies/A/a.mkv"]
    assert stale.ssd
    assert stale.hdd
    fresh = disks.scan_files(ssd, hdd, ["movies"], idx, now=1002.0, marker=".pool", fresh_hdd=True)
    assert fresh["movies/A/a.mkv"].ssd
    assert not fresh["movies/A/a.mkv"].hdd


# ------------------------------------------------------------- small helpers
def test_unmounted_branches_with_marker(pool):
    bare = pool / "bare"
    bare.mkdir()
    got = disks.unmounted_branches([str(pool / "ssd1"), str(bare)], ".pool")
    assert got == [str(bare)]
    assert disks.disk_mounted(str(pool / "ssd1"), ".pool")
    assert not disks.disk_mounted(str(bare))  # not a mount point either


def test_branch_free_skips_unreadable(pool):
    out = disks.branch_free([str(pool / "ssd1"), str(pool / "missing")])
    assert len(out) == 1
    assert out[0][0] > 0
    assert out[0][1] == os.stat(pool / "ssd1").st_dev


def test_pick_branch_most_free_respecting_margin(pool, monkeypatch):
    free = {str(pool / "hdd1"): 100, str(pool / "hdd2"): 300}
    monkeypatch.setattr(disks, "disk_free", lambda b: free[b])
    both = list(free)
    assert disks.pick_branch(both, 50, margin=10) == str(pool / "hdd2")
    assert disks.pick_branch(both, 250, margin=10) == str(pool / "hdd2")
    assert disks.pick_branch(both, 295, margin=10) is None
    assert disks.pick_branch(both, 95, margin=10) == str(pool / "hdd2")
    free[str(pool / "hdd2")] = 20
    assert disks.pick_branch(both, 85, margin=10) == str(pool / "hdd1")
    assert disks.pick_branch(both, 85, margin=20) is None


# ------------------------------------------------------------- copy_verified
def test_copy_verified_success(tmp_path):
    src = make(tmp_path / "a/f.mkv", b"hello" * 100)
    dst = tmp_path / "b/sub/f.mkv"
    disks.copy_verified(str(src), str(dst))
    assert dst.read_bytes() == src.read_bytes()
    assert not [p for p in (tmp_path / "b/sub").iterdir() if p.name.endswith(disks.PART_SUFFIX)]


def test_copy_verified_refuses_hardlink(tmp_path):
    src = make(tmp_path / "a/f.mkv")
    os.link(src, tmp_path / "a/seed.mkv")
    with pytest.raises(disks.Skip, match="hard-linked"):
        disks.copy_verified(str(src), str(tmp_path / "b/f.mkv"))
    assert not (tmp_path / "b/f.mkv").exists()


def test_copy_verified_refuses_recent_file(tmp_path):
    src = make(tmp_path / "a/f.mkv", age=10)
    with pytest.raises(disks.Skip, match="recently"):
        disks.copy_verified(str(src), str(tmp_path / "b/f.mkv"))
    disks.copy_verified(str(src), str(tmp_path / "b/f.mkv"), settle_seconds=5)
    assert (tmp_path / "b/f.mkv").exists()


def test_copy_verified_identical_destination_is_quiet(tmp_path):
    src = make(tmp_path / "a/f.mkv", b"same")
    dst = make(tmp_path / "b/f.mkv", b"same")
    disks.copy_verified(str(src), str(dst))
    assert dst.read_bytes() == b"same"


def test_copy_verified_different_destination_raises(tmp_path):
    src = make(tmp_path / "a/f.mkv", b"aaaa")
    dst = make(tmp_path / "b/f.mkv", b"bbbb")
    with pytest.raises(disks.Skip, match="different"):
        disks.copy_verified(str(src), str(dst))
    assert dst.read_bytes() == b"bbbb"


def test_copy_verified_hash_mismatch_leaves_no_part(tmp_path, monkeypatch):
    src = make(tmp_path / "a/f.mkv", b"data" * 10)
    monkeypatch.setattr(disks, "_sha", lambda path, stop=None: "not-the-hash")
    with pytest.raises(disks.Skip, match="did not match"):
        disks.copy_verified(str(src), str(tmp_path / "b/f.mkv"))
    assert os.listdir(tmp_path / "b") == []
    assert src.read_bytes() == b"data" * 10


def test_copy_verified_stop_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr(disks, "CHUNK", 4)
    src = make(tmp_path / "a/f.mkv", b"0123456789" * 3)
    dst = tmp_path / "b/f.mkv"
    calls = []

    def stop():
        calls.append(1)
        return "enough"

    with pytest.raises(disks.Stopped):
        disks.copy_verified(str(src), str(dst), stop=stop)
    assert len(calls) == 1  # not checked before the first chunk
    assert os.listdir(tmp_path / "b") == []
    assert src.read_bytes() == b"0123456789" * 3


# ----------------------------------------------------------------- move_unit
def _unit(pool):
    make(pool / "ssd1/movies/A/a.mkv", b"a" * 100)
    make(pool / "ssd2/movies/A/extras/b.mkv", b"b" * 50)
    return [str(pool / "ssd1"), str(pool / "ssd2")], [str(pool / "hdd1"), str(pool / "hdd2")]


def test_move_unit_moves_all_and_tidies(pool):
    src, dst = _unit(pool)
    r = disks.move_unit("movies/A", src, dst, margin=0)
    assert r["moved"] == 2
    assert r["bytes"] == 150
    assert r["skipped"] is None
    assert r["to"] in dst
    assert (pathlib.Path(r["to"]) / "movies/A/a.mkv").read_bytes() == b"a" * 100
    assert (pathlib.Path(r["to"]) / "movies/A/extras/b.mkv").read_bytes() == b"b" * 50
    assert not (pool / "ssd1/movies/A").exists()
    assert not (pool / "ssd2/movies/A").exists()
    assert (pool / "ssd1").exists()


def test_move_unit_no_room(pool, monkeypatch):
    src, dst = _unit(pool)
    monkeypatch.setattr(disks, "disk_free", lambda b: 0)
    r = disks.move_unit("movies/A", src, dst, margin=0)
    assert r == {"moved": 0, "bytes": 0, "skipped": "no room on the destination disks"}
    assert (pool / "ssd1/movies/A/a.mkv").exists()


def test_move_unit_nothing_to_move(pool):
    assert disks.move_unit("movies/Z", [str(pool / "ssd1")], [str(pool / "hdd1")])["skipped"] == "nothing to move"


def test_move_unit_stops_when_playing(pool):
    src, dst = _unit(pool)
    r = disks.move_unit("movies/A", src, dst, check_playing=lambda: True, margin=0)
    assert r["moved"] == 0
    assert r["skipped"] == "someone started watching"
    assert (pool / "ssd1/movies/A/a.mkv").exists()
    assert (pool / "ssd2/movies/A/extras/b.mkv").exists()


def test_move_unit_honours_deadline(pool):
    src, dst = _unit(pool)
    r = disks.move_unit("movies/A", src, dst, deadline=1.0, margin=0)
    assert r["moved"] == 0
    assert "out of time" in r["skipped"]
    assert (pool / "ssd1/movies/A/a.mkv").exists()


def test_move_unit_skips_recent_file_and_keeps_it(pool):
    make(pool / "ssd1/movies/A/a.mkv", age=5)
    r = disks.move_unit("movies/A", [str(pool / "ssd1")], [str(pool / "hdd1")], margin=0)
    assert r["moved"] == 0
    assert "recently" in r["skipped"]
    assert (pool / "ssd1/movies/A/a.mkv").exists()


def test_move_unit_reuses_partial_copy_on_other_branch(pool):
    make(pool / "ssd1/movies/A/a.mkv", b"a" * 100)
    make(pool / "hdd2/movies/A/a.mkv", b"a" * 100)  # left by an interrupted run
    dst = [str(pool / "hdd1"), str(pool / "hdd2")]
    r = disks.move_unit("movies/A", [str(pool / "ssd1")], dst, margin=0)
    assert r["moved"] == 1
    assert (pool / "hdd2/movies/A/a.mkv").read_bytes() == b"a" * 100
    assert not (pool / "hdd1/movies/A/a.mkv").exists()
    assert not (pool / "ssd1/movies/A/a.mkv").exists()


def test_move_unit_deletes_identical_stale_duplicate_only(pool):
    make(pool / "ssd1/movies/A/a.mkv", b"a" * 100)
    make(pool / "hdd1/movies/A/a.mkv", b"a" * 100)  # becomes the target (first copy found)
    make(pool / "hdd2/movies/A/a.mkv", b"a" * 100)  # identical stale duplicate
    dst = [str(pool / "hdd1"), str(pool / "hdd2")]
    disks.move_unit("movies/A", [str(pool / "ssd1")], dst, margin=0)
    assert (pool / "hdd1/movies/A/a.mkv").exists()
    assert not (pool / "hdd2/movies/A/a.mkv").exists()

    make(pool / "ssd1/movies/B/b.mkv", b"b" * 100)
    make(pool / "hdd1/movies/B/b.mkv", b"b" * 100)
    make(pool / "hdd2/movies/B/b.mkv", b"DIFFERENT!" * 10)  # same size, other content: kept
    disks.move_unit("movies/B", [str(pool / "ssd1")], dst, margin=0)
    assert (pool / "hdd2/movies/B/b.mkv").read_bytes() == b"DIFFERENT!" * 10
    assert not (pool / "ssd1/movies/B/b.mkv").exists()


def test_move_unit_leaves_whole_unit_when_one_file_cannot_move(pool):
    make(pool / "ssd1/movies/A/a.mkv", b"a" * 100)
    make(pool / "ssd1/movies/A/b.mkv", b"b" * 100, age=5)  # changed recently
    r = disks.move_unit("movies/A", [str(pool / "ssd1")], [str(pool / "hdd1")], margin=0)
    assert r["moved"] == 0
    assert "recently" in r["skipped"]
    assert (pool / "ssd1/movies/A/a.mkv").exists()
    assert not (pool / "hdd1/movies/A").exists()


def test_move_unit_stopping_midway_reports_a_partial_move(pool):
    make(pool / "ssd1/movies/A/a.mkv", b"a" * 100)
    make(pool / "ssd1/movies/A/b.mkv", b"b" * 100)
    calls = []
    r = disks.move_unit(
        "movies/A",
        [str(pool / "ssd1")],
        [str(pool / "hdd1")],
        check_playing=lambda: calls.append(1) or len(calls) > 1,
        margin=0,
    )
    assert r["moved"] == 1
    assert r["skipped"] == "someone started watching"


def test_move_unit_stops_when_a_disk_goes_missing(pool, monkeypatch):
    src, dst = _unit(pool)
    for b in src + dst:
        make(pathlib.Path(b) / ".m" / "x")
    seen = []
    real = disks.copy_verified

    def copy_then_lose_disk(*a, **kw):
        real(*a, **kw)
        seen.append(1)
        for p in (pool / "hdd1" / ".m", pool / "hdd2" / ".m"):
            if p.exists():
                (p / "x").unlink()
                p.rmdir()

    monkeypatch.setattr(disks, "copy_verified", copy_then_lose_disk)
    r = disks.move_unit("movies/A", src, dst, margin=0, marker=".m")
    assert len(seen) == 1
    assert r["moved"] == 1
    assert r["skipped"].startswith(disks.NOT_MOUNTED)
