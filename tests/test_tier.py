"""Whole-tool tests: temp folders stand in for the pool's disks, a fake server for Jellyfin."""

import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from lombo_tier import cli, core, mover, schedule, state
from lombo_tier.config import ConfigError, from_env
from lombo_tier.core import GB, Entry, Settings, Unit, Watch
from lombo_tier.jellyfin import Jellyfin

P = "LOMBO_TIER_"


def iso(ts):
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


@pytest.fixture
def pool(tmp_path, fake):
    ssd, hdd, data = tmp_path / "ssd1", tmp_path / "hdd1", tmp_path / "data"
    for d in (ssd, hdd):
        (d / ".m").mkdir(parents=True)  # the disk marker: stands in for a real mount point
    env = {
        P + "JELLYFIN_URL": fake.url,
        P + "JELLYFIN_API_KEY": "good-key",
        P + "JELLYFIN_PATH_PREFIX": "/media",
        P + "SSD_BRANCHES": str(ssd),
        P + "HDD_BRANCHES": str(hdd),
        P + "DATA_DIR": str(data),
        P + "DISK_MARKER": ".m",
        P + "RESERVE_GB": "0",
        P + "HEADROOM_GB": "1000000",  # "always short of room"
        P + "MIN_AGE_DAYS": "0",
        P + "DEST_MARGIN_GB": "0",
        P + "WAIT_QUIET_MINUTES": "0",
        P + "WEBHOOK_URL": fake.url + "/hook",
    }

    def add(branch, title, size=2048):
        d = branch / "movies" / title
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{title}.mkv"
        f.write_bytes(b"x" * size)
        old = time.time() - 3 * 86400
        os.utime(f, (old, old))
        fake.items.append({"Path": f"/media/movies/{title}/{title}.mkv", "DateCreated": iso(time.time() - 30 * 86400)})
        return f

    def watched(title, plays=2):
        fake.users = [{"Id": "u1", "Name": "a"}]
        fake.user_items = {
            "u1": {
                "IsPlayed": [
                    {
                        "Path": f"/media/movies/{title}/{title}.mkv",
                        "UserData": {"LastPlayedDate": iso(time.time() - 86400), "PlayCount": plays},
                    }
                ],
                "IsResumable": [],
            }
        }

    def playing(title):
        fake.sessions = [{"NowPlayingItem": {"Path": f"/media/movies/{title}/{title}.mkv"}}]

    return SimpleNamespace(
        ssd=ssd,
        hdd=hdd,
        data=data,
        env=env,
        add=add,
        watched=watched,
        playing=playing,
        cfg=lambda **kw: from_env({**env, **{P + k: v for k, v in kw.items()}}),
        jf=lambda cfg: Jellyfin(cfg.jellyfin_url, cfg.jellyfin_api_key),
    )


# ------------------------------------------------------------------ the run
def test_dry_run_plans_but_moves_nothing_and_saves_no_state(pool):
    f = pool.add(pool.ssd, "Cold (2001)")
    cfg = pool.cfg()
    assert cfg.dry_run is True  # safe by default
    result = mover.run(cfg, pool.jf(cfg))
    assert [m["rel"] for m in result["plan"]["demote"]] == ["movies/Cold (2001)"]
    assert f.exists()
    assert not (pool.hdd / "movies").exists()
    assert not os.path.exists(cfg.state_path)


def test_live_run_moves_cold_title_to_hdd_and_records_cooldown(pool):
    pool.add(pool.ssd, "Cold (2001)")
    cfg = pool.cfg(DRY_RUN="false")
    result = mover.run(cfg, pool.jf(cfg), wait=False)
    assert result["note"] == "ok"
    assert [d["unit"] for d in result["demoted"]] == ["movies/Cold (2001)"]
    assert (pool.hdd / "movies/Cold (2001)/Cold (2001).mkv").read_bytes() == b"x" * 2048
    assert not (pool.ssd / "movies/Cold (2001)").exists()
    saved = state.load(cfg.state_path)
    assert saved["cooldown"]["movies/Cold (2001)"]["dir"] == "demote"
    assert saved["last_run"]["note"] == "ok"


def test_hot_title_on_hdd_comes_back_to_ssd(pool):
    pool.add(pool.hdd, "Hot (2020)")
    pool.watched("Hot (2020)")
    cfg = pool.cfg(DRY_RUN="false")
    result = mover.run(cfg, pool.jf(cfg), wait=False)
    assert [p["unit"] for p in result["promoted"]] == ["movies/Hot (2020)"]
    assert (pool.ssd / "movies/Hot (2020)/Hot (2020).mkv").exists()
    assert not (pool.hdd / "movies/Hot (2020)").exists()
    assert state.load(cfg.state_path)["cooldown"]["movies/Hot (2020)"]["dir"] == "promote"


def test_a_title_that_is_playing_is_never_moved(pool):
    a = pool.add(pool.ssd, "Playing (1999)")
    pool.playing("Playing (1999)")
    cfg = pool.cfg(DRY_RUN="false")
    result = mover.run(cfg, pool.jf(cfg), wait=False)
    assert a.exists()
    assert result["demoted"] == []


def test_nothing_moves_if_people_watch_the_whole_wait(pool):
    cold = pool.add(pool.ssd, "Cold (2001)")
    pool.add(pool.ssd, "Other (2002)")
    pool.playing("Other (2002)")  # someone is watching a different title
    cfg = pool.cfg(DRY_RUN="false")
    result = mover.run(cfg, pool.jf(cfg), wait=True, sleep=lambda s: None)
    assert "watching the whole time" in result["note"]
    assert cold.exists()


def test_unmounted_disk_stops_everything_and_is_a_failure(pool):
    f = pool.add(pool.ssd, "Cold (2001)")
    (pool.hdd / ".m").rmdir()  # the HDD "did not mount"
    cfg = pool.cfg(DRY_RUN="false")
    result = mover.run(cfg, pool.jf(cfg), wait=False)
    assert result["failed"]
    assert "not mounted" in result["note"]
    assert f.exists()


def test_unreadable_state_file_stops_the_run_and_is_left_alone(pool):
    f = pool.add(pool.ssd, "Cold (2001)")
    pool.data.mkdir()
    (pool.data / "state.json").write_text("{ not json")
    cfg = pool.cfg(DRY_RUN="false")
    result = mover.run(cfg, pool.jf(cfg), wait=False)
    assert result["failed"]
    assert f.exists()
    assert (pool.data / "state.json").read_text() == "{ not json"


def test_recently_promoted_title_is_not_pushed_back_down(pool):
    pool.add(pool.ssd, "Cold (2001)")
    pool.data.mkdir()
    state.save(
        str(pool.data / "state.json"), {"cooldown": {"movies/Cold (2001)": {"dir": "promote", "at": time.time()}}}
    )
    cfg = pool.cfg(DRY_RUN="false")
    result = mover.run(cfg, pool.jf(cfg), wait=False)
    assert result["demoted"] == []


def test_main_run_posts_a_summary_to_the_webhook(pool, fake):
    pool.add(pool.ssd, "Cold (2001)")
    assert cli.main(["run", "--no-wait"], {**pool.env, P + "DRY_RUN": "false"}) == 0
    assert "Moved to HDD: 1 title(s)" in fake.posts()[0]["body"]["content"]


def test_main_run_in_dry_mode_says_so_and_stays_quiet(pool, fake, capsys):
    f = pool.add(pool.ssd, "Cold (2001)")
    assert cli.main(["run", "--no-wait"], pool.env) == 0
    out = capsys.readouterr().out
    assert "dry run, nothing moved" in out
    assert "movies/Cold (2001)" in out
    assert f.exists()
    assert fake.posts() == []


# -------------------------------------------------------------- move command
def test_move_resolves_names_and_moves_to_hdd(pool):
    pool.add(pool.ssd, "Cold (2001)")
    cfg = pool.cfg(DRY_RUN="false")
    assert mover.resolve_units(cfg, ["Cold (2001)"]) == ["movies/Cold (2001)"]
    assert mover.resolve_units(cfg, ["movies/Cold (2001)"]) == ["movies/Cold (2001)"]
    result = mover.run_move(cfg, pool.jf(cfg), ["Cold (2001)"], wait=False)
    assert [d["unit"] for d in result["demoted"]] == ["movies/Cold (2001)"]
    assert (pool.hdd / "movies/Cold (2001)/Cold (2001).mkv").exists()


def test_move_rejects_a_missing_name_and_dry_run_moves_nothing(pool, capsys):
    f = pool.add(pool.ssd, "Cold (2001)")
    cfg = pool.cfg()
    with pytest.raises(ValueError, match="not found"):
        mover.resolve_units(cfg, ["Nope"])
    assert cli.main(["move", "Nope"], pool.env) == 2
    assert cli.main(["move", "Cold (2001)"], pool.env) == 0  # dry run by default
    assert f.exists()
    assert "dry run" in capsys.readouterr().out


# ------------------------------------------------------------ doctor and plan
def test_doctor_passes_on_a_healthy_setup(pool, capsys):
    pool.add(pool.ssd, "Cold (2001)")
    assert cli.main(["doctor"], pool.env) == 0
    out = capsys.readouterr().out
    assert "[FAIL]" not in out
    assert "Jellyfin's paths start with /media" in out
    assert "[warn] library folder tv" in out  # a movies-only library is fine


def test_doctor_fails_when_no_library_folder_exists_anywhere(pool, capsys):
    assert cli.main(["doctor"], {**pool.env, P + "MOVIE_ROOTS": "film", P + "SERIES_ROOTS": "shows"}) == 1
    assert "none of film, shows exist on any disk" in capsys.readouterr().out


def test_doctor_suggests_the_right_path_prefix(pool, capsys):
    pool.add(pool.ssd, "Cold (2001)")
    assert cli.main(["doctor"], {**pool.env, P + "JELLYFIN_PATH_PREFIX": "/wrong"}) == 1
    assert "LOMBO_TIER_JELLYFIN_PATH_PREFIX=/media" in capsys.readouterr().out


def test_doctor_flags_an_unmounted_disk_and_a_wrong_key(pool, capsys):
    (pool.hdd / ".m").rmdir()
    assert cli.main(["doctor"], {**pool.env, P + "JELLYFIN_API_KEY": "bad"}) == 1
    out = capsys.readouterr().out
    assert "not mounted" in out
    assert "API key accepted: /System/Info: HTTP 401" in out


def test_plan_prints_and_never_changes_anything(pool, capsys):
    f = pool.add(pool.ssd, "Cold (2001)")
    assert cli.main(["plan"], {**pool.env, P + "DRY_RUN": "false"}) == 0
    assert "Move to HDD (1):" in capsys.readouterr().out
    assert f.exists()
    assert cli.main(["plan", "--json"], pool.env) == 0
    assert '"demote"' in capsys.readouterr().out


def test_plan_labels_the_most_watched_titles_honestly(pool):
    plan = core.Plan(popular=[{"unit": "movies/A (2001)", "viewers": 1, "where": "ssd"}])
    text = cli.format_plan(plan, 10 * GB, from_env(pool.env))
    assert "Most watched recently: movies/A (2001) (1 viewers, on ssd)" in text
    assert "Popular" not in text


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_doctor_hints_at_a_read_only_mount(pool, capsys):
    pool.ssd.chmod(0o555)
    try:
        cli.main(["doctor"], pool.env)
    finally:
        pool.ssd.chmod(0o755)
    assert "not writable by this user (a read-only mount, or wrong owner or permissions)" in capsys.readouterr().out


# -------------------------------------------------------------------- config
def test_config_rejects_bad_values_and_names_the_setting(pool):
    for name, value in [
        ("JELLYFIN_URL", "jellyfin:8096"),
        ("JELLYFIN_API_KEY", "changeme"),
        ("JELLYFIN_PATH_PREFIX", ""),
        ("SSD_BRANCHES", ""),
        ("HDD_BRANCHES", "relative/path"),
        ("SSD_BRANCHES", str(pool.hdd)),
        ("MOVIE_ROOTS", "../etc"),
        ("SERIES_ROOTS", "movies"),
        ("RUN_HOUR", "24"),
        ("RESERVE_GB", "-1"),
        ("MAX_GB_PER_RUN", "0"),
        ("DISK_MARKER", "a/b"),
        ("WEBHOOK_URL", "ftp://x"),
    ]:
        with pytest.raises(ConfigError, match=P + name):
            from_env({**pool.env, P + name: value})


def test_config_defaults(pool):
    cfg = pool.cfg()
    assert cfg.dry_run
    assert cfg.roots == ("movies", "tv")
    assert cfg.settings.min_age_days == 0
    assert from_env({k: v for k, v in pool.env.items() if "MIN_AGE" not in k}).settings.min_age_days == 14


# --------------------------------------------------------------- pure rules
S = Settings(reserve=10 * GB, headroom=10 * GB, min_age_days=14, pin_days=30)
NOW = 1_000_000_000.0


def unit(rel, **kw):
    return Unit(rel, "movie", **{"ssd_bytes": 5 * GB, "added": NOW - 90 * 86400, **kw})


def test_unit_of_uses_configurable_roots():
    assert core.unit_of("movies/Jaws/jaws.mkv", ["tv"], ["movies"]) == ("movies/Jaws", "movie", None, None)
    assert core.unit_of("library/tv/Show/Season 02/e1.mkv", ["library/tv"], []) == (
        "library/tv/Show/Season 02",
        "season",
        "library/tv/Show",
        2,
    )
    assert core.unit_of("tv/Show/e1.mkv", ["tv"], []) is None  # loose file at a show's root
    assert core.unit_of("tv/Show/Extras/e1.mkv", ["tv"], []) is None  # not a season folder
    assert core.unit_of("other/Jaws/jaws.mkv", ["tv"], ["movies"]) is None


def test_paths_outside_the_prefix_are_ignored_and_prefix_is_suggested():
    assert core.rel_from_media_path("/media/movies/a.mkv", "/media") == "movies/a.mkv"
    assert core.rel_from_media_path("/other/movies/a.mkv", "/media") is None
    assert core.suggest_prefix("/mnt/stuff/movies/x/x.mkv", ["tv", "movies"]) == "/mnt/stuff"


def test_demotes_coldest_first_and_stops_at_the_target():
    units = {
        "a": unit("a", last_played=NOW - 200 * 86400),
        "b": unit("b"),
        "c": unit("c", last_played=NOW - 10 * 86400),
    }
    p = core.plan(units, ssd_free=12 * GB, s=S, now=NOW)  # target 20 GB free: need 8 GB, so two 5 GB moves
    assert [m.rel for m in p.demote] == ["b", "a"]  # never watched first, then longest unwatched


def test_nothing_moves_while_there_is_room_and_young_or_held_titles_stay():
    assert "room" in core.plan({"a": unit("a")}, ssd_free=50 * GB, s=S, now=NOW).notes[0]
    held = {"a": unit("a", held=True), "b": unit("b", added=NOW - 86400), "c": unit("c", playing=True)}
    assert core.plan(held, ssd_free=1 * GB, s=S, now=NOW).demote == []


def test_promote_needs_room_and_respects_the_run_limit():
    hot = {"a": Unit("a", "movie", hdd_bytes=30 * GB, hot_reason="played 2 times this month")}
    assert core.plan(hot, ssd_free=12 * GB, s=S, now=NOW).promote == []  # 12 - 30 < reserve
    assert [m.rel for m in core.plan(hot, ssd_free=100 * GB, s=S, now=NOW).promote] == ["a"]
    small = Settings(10 * GB, 10 * GB, max_bytes=5 * GB)
    assert core.plan(hot, ssd_free=100 * GB, s=small, now=NOW).promote == []


def test_series_binge_brings_the_season_and_the_next_one_back():
    seasons = {f"tv/S/Season 0{n}": Unit(f"tv/S/Season 0{n}", "season", "tv/S", n, hdd_bytes=GB) for n in (1, 2, 3)}
    apply = {
        f"tv/S/Season 01/e{i}.mkv": Watch(last_played=NOW - 86400, plays=1, viewers={"u": NOW - 86400}) for i in (1, 2)
    }
    core.apply_watch(seasons, apply, set(), ["tv"], [], NOW)
    core.mark_hot(seasons, S, NOW)
    assert [bool(u.hot_reason) for u in seasons.values()] == [True, True, False]


def test_effective_free_counts_space_above_each_reserve_and_one_filesystem_once():
    assert core.effective_free([(100, 1), (100, 1), (50, 2)], 10) == 140
    assert core.effective_free([], 10) == 0


def test_units_are_totalled_from_the_file_index():
    files = {
        "movies/A/a.mkv": Entry(100, True, False, 1, 5.0),
        "movies/A/a.srt": Entry(5, True, False, 2, 9.0),
        "movies/B/b.mkv": Entry(50, False, True, 1, 7.0),
    }
    units = core.units_from_files(files, [], ["movies"])
    assert (units["movies/A"].ssd_bytes, units["movies/A"].held) == (105, True)
    assert units["movies/B"].hdd_bytes == 50


# -------------------------------------------------------- schedule and state
def test_next_run_follows_the_local_clock_across_daylight_saving():
    # In America/Edmonton the clocks go forward at 02:00 local (09:00 UTC) on 2026-03-08.
    def utc(day, h, m=0):
        return datetime(2026, 3, day, h, m, tzinfo=UTC).timestamp()

    assert schedule.seconds_until(4, utc(7, 10), "America/Edmonton") == pytest.approx(
        3600
    )  # 03:00 MST, 04:00 is 1 h away
    assert schedule.seconds_until(4, utc(8, 9, 30), "America/Edmonton") == pytest.approx(
        1800
    )  # 01:30 MST, clocks jump: 30 min
    assert schedule.seconds_until(4, utc(8, 10), "America/Edmonton") == pytest.approx(
        86400
    )  # exactly 04:00 MDT: next one is tomorrow
    assert schedule.seconds_until(4, utc(8, 10), "UTC") == pytest.approx(18 * 3600)
    assert schedule.seconds_until(4, utc(8, 10), "Not/AZone") == pytest.approx(
        18 * 3600
    )  # unknown zone falls back to UTC


def test_state_roundtrip_and_problem_detection(tmp_path):
    path = str(tmp_path / "d" / "state.json")
    assert state.problem(path) is None
    assert state.load(path) == {}
    state.save(path, {"a": 1})
    assert state.load(path) == {"a": 1}
    assert state.problem(path) is None
    Path(path).write_text("[1]")
    assert state.problem(path) == "not a JSON object"


def test_env_example_is_valid_and_lists_every_setting():
    root = Path(__file__).resolve().parent.parent
    example = dict(
        line.split("=", 1) for line in (root / ".env.example").read_text().splitlines() if line.startswith(P)
    )
    from_env({**example, P + "JELLYFIN_API_KEY": "a-real-key"})  # the example file must parse as it is
    read = set(
        re.findall(r'(?:get|number|url|folders)\(\s*"([A-Z_]+)"', (root / "src/lombo_tier/config.py").read_text())
    )
    assert read, "the pattern no longer finds any settings in config.py"
    assert {P + name for name in read} <= set(example), "a setting is missing from .env.example"


def test_move_rejects_a_name_that_climbs_out_of_the_library(pool, capsys):
    f = pool.add(pool.ssd, "Cold (2001)")
    live = {**pool.env, P + "DRY_RUN": "false"}
    for bad in ("movies/..", "..", "movies/../movies/Cold (2001)"):
        with pytest.raises(ValueError, match="not allowed"):
            mover.resolve_units(pool.cfg(), [bad])
        assert cli.main(["move", bad], live) == 2
    assert f.exists()
    assert not (pool.hdd / "movies").exists()


def test_run_stops_when_a_disk_goes_missing_mid_run(pool, monkeypatch):
    a = pool.add(pool.ssd, "Cold (2001)")
    b = pool.add(pool.ssd, "Colder (2002)")
    real = mover.disks.copy_verified

    def copy_then_lose_disk(*args, **kw):
        real(*args, **kw)
        if (pool.hdd / ".m").exists():
            (pool.hdd / ".m").rmdir()

    monkeypatch.setattr(mover.disks, "copy_verified", copy_then_lose_disk)
    cfg = pool.cfg(DRY_RUN="false")
    result = mover.run(cfg, pool.jf(cfg), wait=False)
    assert result["failed"]
    assert "not mounted" in result["note"]
    assert len(result["demoted"]) == 1
    assert [a.exists(), b.exists()].count(True) == 1  # the other title was never touched


def test_half_moved_unit_gets_no_cooldown(pool, monkeypatch):
    pool.add(pool.ssd, "Cold (2001)")
    monkeypatch.setattr(
        mover.disks, "move_unit", lambda *a, **kw: {"moved": 1, "bytes": 5, "skipped": "someone started watching"}
    )
    cfg = pool.cfg(DRY_RUN="false")
    mover.run(cfg, pool.jf(cfg), wait=False)
    assert "movies/Cold (2001)" not in state.load(cfg.state_path).get("cooldown", {})
