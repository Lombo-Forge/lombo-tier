"""move NAME, pinned titles, tiers.json and the conversion-session lock. Folders only, never a real mount."""

import json
import os
import time

import pytest

from lombo_tier import cli, core, mover, pins, session, state, tiers
from lombo_tier.config import ConfigError, from_env
from test_tier import P, pool  # noqa: F401  (the fixture)

TITLE = "Cold (2001)"
UNIT = "movies/Cold (2001)"


def live(pool, **extra):  # noqa: F811
    return {**pool.env, P + "DRY_RUN": "false", **{P + k: v for k, v in extra.items()}}


def read_tiers(cfg):
    with open(cfg.tiers_path) as f:
        return json.load(f)


def write_json(path, data):
    path.write_text(json.dumps(data))
    return str(path)


# ------------------------------------------------------------ A3: move NAME
def test_move_dry_run_flag_prints_the_plan_and_changes_nothing(pool, capsys):  # noqa: F811
    f = pool.add(pool.ssd, TITLE)
    assert cli.main(["move", TITLE, "--dry-run"], live(pool)) == 0  # DRY_RUN=false, flag still wins
    assert "Would move to HDD: " + UNIT in capsys.readouterr().out
    assert f.exists()
    assert not (pool.hdd / "movies").exists()
    assert not os.path.exists(pool.data / "state.json")


def test_move_several_names_records_the_run_like_a_night(pool):  # noqa: F811
    pool.add(pool.ssd, TITLE)
    pool.add(pool.ssd, "Other (2002)")
    cfg = pool.cfg(DRY_RUN="false")
    result = mover.run_move(cfg, pool.jf(cfg), [TITLE, "movies/Other (2002)"], wait=False)
    assert sorted(d["unit"] for d in result["demoted"]) == [UNIT, "movies/Other (2002)"]
    saved = state.load(cfg.state_path)
    assert saved["last_run"]["note"] == "manual move: ok"
    assert saved["cooldown"][UNIT]["dir"] == "demote"


def test_move_with_an_ambiguous_name_lists_the_matches_and_moves_nothing(pool, capsys):  # noqa: F811
    f = pool.add(pool.ssd, "Dup (2000)")
    season = pool.ssd / "tv/Dup (2000)/Season 1"
    season.mkdir(parents=True)
    (season / "e1.mkv").write_bytes(b"x")
    assert cli.main(["move", "Dup (2000)"], live(pool)) == 2
    err = capsys.readouterr().err
    assert "matches several" in err
    assert "movies/Dup (2000)" in err
    assert "tv/Dup (2000)" in err
    assert f.exists()
    assert not (pool.hdd / "movies").exists()


def test_move_with_a_missing_name_moves_nothing(pool, capsys):  # noqa: F811
    pool.add(pool.ssd, TITLE)
    assert cli.main(["move", TITLE, "Nope (1999)"], live(pool)) == 2  # one bad name stops the whole command
    assert "not found" in capsys.readouterr().err
    assert (pool.ssd / UNIT).exists()


def test_move_never_moves_a_title_that_is_playing_even_with_no_wait(pool):  # noqa: F811
    f = pool.add(pool.ssd, TITLE)
    pool.playing(TITLE)
    cfg = pool.cfg(DRY_RUN="false")
    result = mover.run_move(cfg, pool.jf(cfg), [TITLE], wait=False)
    assert f.exists()
    assert result["demoted"] == []
    assert result["skipped"] == [{"unit": UNIT, "why": "playing now"}]


def test_move_leaves_a_title_that_is_still_seeding(pool, tmp_path):  # noqa: F811
    f = pool.add(pool.ssd, TITLE)
    os.link(f, tmp_path / "seeding.mkv")
    cfg = pool.cfg(DRY_RUN="false")
    result = mover.run_move(cfg, pool.jf(cfg), [TITLE], wait=False)
    assert f.exists()
    assert "hard-linked" in result["skipped"][0]["why"]


def test_move_keeps_the_destination_margin(pool):  # noqa: F811
    f = pool.add(pool.ssd, TITLE)
    cfg = pool.cfg(DRY_RUN="false", DEST_MARGIN_GB="100000000")
    result = mover.run_move(cfg, pool.jf(cfg), [TITLE], wait=False)
    assert f.exists()
    assert "no room" in result["skipped"][0]["why"]


def test_move_stops_when_a_disk_is_not_mounted(pool):  # noqa: F811
    f = pool.add(pool.ssd, TITLE)
    (pool.hdd / ".m").rmdir()
    cfg = pool.cfg(DRY_RUN="false")
    result = mover.run_move(cfg, pool.jf(cfg), [TITLE], wait=False)
    assert result["failed"]
    assert "not mounted" in result["note"]
    assert f.exists()


def test_a_second_run_or_move_at_the_same_time_does_not_start(pool, capsys):  # noqa: F811
    f = pool.add(pool.ssd, TITLE)
    cfg = pool.cfg(DRY_RUN="false")
    fd = mover._acquire_lock(cfg.lock_path)  # another process is moving
    try:
        moved = mover.run_move(cfg, pool.jf(cfg), [TITLE], wait=False)
        ran = mover.run(cfg, pool.jf(cfg), wait=False)
        assert moved["busy"]
        assert ran["busy"]
        assert cli.main(["move", TITLE], live(pool)) == 1
        assert "in progress" in capsys.readouterr().err
        assert f.exists()
        assert not os.path.exists(cfg.state_path)
        # a dry run changes nothing, so it does not need the lock
        assert "busy" not in mover.run_move(cfg, pool.jf(cfg), [TITLE], dry_run=True)
    finally:
        os.close(fd)
    assert mover.run_move(cfg, pool.jf(cfg), [TITLE], wait=False)["demoted"]  # the lock was released


def test_an_unwritable_lock_stands_down(pool):  # noqa: F811
    pool.add(pool.ssd, TITLE)
    pool.data.mkdir()
    (pool.data / "run.lock").mkdir()  # a folder where the lock file should be
    cfg = pool.cfg(DRY_RUN="false")
    result = mover.run(cfg, pool.jf(cfg), wait=False)
    assert result["failed"]
    assert "run lock" in result["note"]


# ----------------------------------------------------------- A4: pinned titles
def test_pin_add_list_remove_round_trip(pool, tmp_path, capsys):  # noqa: F811
    pool.add(pool.ssd, TITLE)
    pin_file = tmp_path / "pins.json"
    env = {**pool.env, P + "PIN_FILE": str(pin_file)}
    assert cli.main(["pin", "list"], env) == 0
    assert "no pinned titles" in capsys.readouterr().out
    assert cli.main(["pin", "add", TITLE], env) == 0
    assert json.loads(pin_file.read_text()) == {"version": 1, "titles": [UNIT]}  # stored as a library path
    assert not os.path.exists(str(pin_file) + ".tmp")  # written atomically
    assert cli.main(["pin", "add", TITLE], env) == 0  # twice is still once
    assert json.loads(pin_file.read_text())["titles"] == [UNIT]
    assert cli.main(["pin", "list"], env) == 0
    assert UNIT in capsys.readouterr().out
    assert cli.main(["pin", "remove", TITLE], env) == 0
    assert json.loads(pin_file.read_text()) == {"version": 1, "titles": []}


def test_pin_add_refuses_unknown_ambiguous_and_non_title_names(pool, tmp_path, capsys):  # noqa: F811
    pool.add(pool.hdd, "Dup (2000)")  # pinning works for a title on any disk
    (pool.ssd / "tv/Dup (2000)/Season 1").mkdir(parents=True)
    (pool.ssd / "tv/Show/Season 2").mkdir(parents=True)
    pin_file = tmp_path / "pins.json"
    env = {**pool.env, P + "PIN_FILE": str(pin_file)}
    assert cli.main(["pin", "add", "Nope"], env) == 2
    assert cli.main(["pin", "add", "Dup (2000)"], env) == 2
    assert "matches several" in capsys.readouterr().err
    assert cli.main(["pin", "add", "movies"], env) == 2  # a library folder is not a title
    assert not pin_file.exists()
    assert cli.main(["pin", "add", "movies/Dup (2000)", "tv/Show", "tv/Show/Season 2"], env) == 0  # movie, show, season
    assert len(json.loads(pin_file.read_text())["titles"]) == 3


def test_pin_remove_of_a_title_that_is_not_pinned_says_what_is(pool, tmp_path, capsys):  # noqa: F811
    pin_file = write_json(tmp_path / "pins.json", {"version": 1, "titles": [UNIT]})
    env = {**pool.env, P + "PIN_FILE": pin_file}
    assert cli.main(["pin", "remove", "Other"], env) == 2
    assert UNIT in capsys.readouterr().err
    assert cli.main(["pin", "remove", "Cold (2001)"], env) == 0  # a bare name finds the library path


def test_pin_commands_need_the_setting_and_a_name(pool, capsys):  # noqa: F811
    assert cli.main(["pin", "list"], pool.env) == 2
    assert "LOMBO_TIER_PIN_FILE" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["pin", "add"], {**pool.env, P + "PIN_FILE": "/x/pins.json"})


def test_a_pinned_title_is_skipped_by_the_plan_and_the_run(pool, tmp_path):  # noqa: F811
    pinned = pool.add(pool.ssd, TITLE)
    plain = pool.add(pool.ssd, "Plain (2003)")
    pin_file = write_json(tmp_path / "pins.json", {"version": 1, "titles": [TITLE]})  # a bare name works too
    cfg = pool.cfg(DRY_RUN="false", PIN_FILE=pin_file)
    p, _units, _free = mover.make_plan(cfg, pool.jf(cfg))
    assert [m.rel for m in p.demote] == ["movies/Plain (2003)"]
    result = mover.run(cfg, pool.jf(cfg), wait=False)
    assert [d["unit"] for d in result["demoted"]] == ["movies/Plain (2003)"]
    assert pinned.exists()
    assert not plain.exists()


def test_a_pinned_title_is_not_moved_by_move(pool, tmp_path, capsys):  # noqa: F811
    f = pool.add(pool.ssd, TITLE)
    pin_file = write_json(tmp_path / "pins.json", {"version": 1, "titles": [UNIT]})
    env = live(pool, PIN_FILE=pin_file)
    assert cli.main(["move", TITLE, "--dry-run"], env) == 0
    assert "pinned" in capsys.readouterr().out
    assert cli.main(["move", TITLE], env) == 0
    assert "pinned" in capsys.readouterr().out
    assert f.exists()
    assert not (pool.hdd / "movies").exists()


def test_pinning_a_show_covers_its_seasons():
    roots = ("movies", "tv")
    assert pins.is_pinned("tv/Show/Season 1", ["Show"], roots)
    assert pins.is_pinned("tv/Show/Season 1", ["tv/Show"], roots)
    assert pins.is_pinned("tv/Show/Season 1", ["tv/Show/Season 1"], roots)
    assert not pins.is_pinned("tv/Show/Season 2", ["tv/Show/Season 1"], roots)
    assert not pins.is_pinned("tv/Show 2/Season 1", ["Show"], roots)  # a prefix of the name is not a match
    assert pins.is_pinned("movies/Jaws (1975)", ["Jaws (1975)"], roots)


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        '{"version": 2, "titles": []}',
        '{"version": 1, "titles": "Jaws"}',
        '{"version": 1, "titles": [""]}',
        '{"version": 1, "titles": ["movies/../x"]}',
        '{"version": 1}',
        "",
        "\x00",
    ],
)
def test_a_damaged_pin_list_stands_everything_down(pool, tmp_path, content, capsys):  # noqa: F811
    f = pool.add(pool.ssd, TITLE)
    pin_file = tmp_path / "pins.json"
    pin_file.write_text(content)
    assert pins.problem(str(pin_file))
    with pytest.raises(ValueError, match="can't be read"):
        pins.load(str(pin_file))
    cfg = pool.cfg(DRY_RUN="false", PIN_FILE=str(pin_file))
    ran = mover.run(cfg, pool.jf(cfg), wait=False)
    moved = mover.run_move(cfg, pool.jf(cfg), [TITLE], wait=False)
    for result in (ran, moved):
        assert result["failed"]
        assert "pin list can't be read" in result["note"]
        assert result["demoted"] == []
    assert f.exists()
    assert not os.path.exists(cfg.state_path)  # stood down without touching the state file
    assert not os.path.exists(cfg.tiers_path)
    env = {**pool.env, P + "PIN_FILE": str(pin_file)}
    assert cli.main(["plan"], env) == 0
    out = capsys.readouterr().out
    assert "standing down" in out
    assert "Move to HDD (0)" in out
    assert cli.main(["doctor"], env) == 1
    assert "[FAIL] pin list" in capsys.readouterr().out
    assert cli.main(["pin", "add", TITLE], env) == 1  # and the list is never overwritten
    assert pin_file.read_text() == content


def test_a_missing_pin_file_is_just_an_empty_list(pool, tmp_path):  # noqa: F811
    pool.add(pool.ssd, TITLE)
    cfg = pool.cfg(DRY_RUN="false", PIN_FILE=str(tmp_path / "nope.json"))
    assert pins.problem(cfg.pin_file) is None
    assert mover.run(cfg, pool.jf(cfg), wait=False)["demoted"]


def two_ssds(pool, tmp_path, pins_list, **extra):  # noqa: F811
    ssd2 = tmp_path / "ssd2"
    (ssd2 / ".m").mkdir(parents=True)
    pin_file = write_json(tmp_path / "pins.json", {"version": 1, "titles": pins_list})
    return ssd2, pool.cfg(
        DRY_RUN="false",
        SSD_BRANCHES=f"{pool.ssd},{ssd2}",
        PIN_FILE=pin_file,
        PIN_BRANCH=str(ssd2),
        **extra,
    )


def test_pinned_titles_are_gathered_onto_the_pin_branch(pool, tmp_path):  # noqa: F811
    on_ssd1 = pool.add(pool.ssd, TITLE)
    on_hdd = pool.add(pool.hdd, "Away (1999)")
    already = pool.add(pool.ssd, "Plain (2003)")
    ssd2, cfg = two_ssds(pool, tmp_path, [TITLE, "Away (1999)"])
    p, _units, _free = mover.make_plan(cfg, pool.jf(cfg))
    assert sorted(m.rel for m in p.gather) == ["movies/Away (1999)", UNIT]
    result = mover.run(cfg, pool.jf(cfg), wait=False)
    assert sorted(d["unit"] for d in result["gathered"]) == ["movies/Away (1999)", UNIT]
    assert (ssd2 / UNIT / f"{TITLE}.mkv").exists()
    assert (ssd2 / "movies/Away (1999)/Away (1999).mkv").exists()
    assert not on_ssd1.exists()
    assert not on_hdd.exists()
    assert not already.exists()  # the unpinned one was demoted as usual
    assert state.load(cfg.state_path).get("cooldown", {}).keys() == {"movies/Plain (2003)"}  # no cooldown for a gather
    # the next run has nothing left to gather
    again = mover.run(cfg, pool.jf(cfg), wait=False)
    assert again["gathered"] == []
    assert (ssd2 / UNIT).exists()
    assert "Gathered pinned titles: 2 title(s)" in mover.summary(result)


def test_with_a_missing_pin_branch_nothing_about_pinned_titles_moves(pool, tmp_path):  # noqa: F811
    f = pool.add(pool.ssd, TITLE)
    away = pool.add(pool.hdd, "Away (1999)")
    ssd2 = tmp_path / "ssd2"  # never created
    pin_file = write_json(tmp_path / "pins.json", {"version": 1, "titles": [TITLE, "Away (1999)"]})
    cfg = pool.cfg(
        DRY_RUN="false",
        CHECK_MOUNTS="false",
        SSD_BRANCHES=f"{pool.ssd},{ssd2}",
        PIN_FILE=pin_file,
        PIN_BRANCH=str(ssd2),
    )
    p, _units, _free = mover.make_plan(cfg, pool.jf(cfg))
    assert p.gather == []
    assert any("pin branch" in n and "not moving pinned titles" in n for n in p.notes)
    result = mover.run(cfg, pool.jf(cfg), wait=False)
    assert result["gathered"] == []
    assert result["demoted"] == []
    assert f.exists()  # not demoted, not gathered
    assert away.exists()


def test_with_an_unmounted_pin_branch_the_whole_run_stands_down(pool, tmp_path):  # noqa: F811
    f = pool.add(pool.ssd, TITLE)
    ssd2, cfg = two_ssds(pool, tmp_path, [TITLE])
    (ssd2 / ".m").rmdir()
    result = mover.run(cfg, pool.jf(cfg), wait=False)
    assert result["failed"]
    assert "not mounted" in result["note"]
    assert f.exists()


def test_doctor_reports_the_pin_branch(pool, tmp_path, capsys):  # noqa: F811
    pool.add(pool.ssd, TITLE)
    ssd2, cfg = two_ssds(pool, tmp_path, [TITLE])
    env = {
        **pool.env,
        P + "SSD_BRANCHES": f"{pool.ssd},{ssd2}",
        P + "PIN_FILE": cfg.pin_file,
        P + "PIN_BRANCH": str(ssd2),
    }
    assert cli.main(["doctor"], env) == 0
    out = capsys.readouterr().out
    assert "[ ok ] pin list" in out
    assert "1 pinned title(s)" in out
    assert f"[ ok ] pin branch {ssd2}" in out
    (ssd2 / ".m").rmdir()
    assert cli.main(["doctor"], env) == 1


def test_pin_settings_are_checked(pool, tmp_path):  # noqa: F811
    ssd = str(pool.ssd)
    with pytest.raises(ConfigError, match="PIN_BRANCH must be one of"):
        pool.cfg(PIN_FILE="/p.json", PIN_BRANCH="/elsewhere")
    with pytest.raises(ConfigError, match="PIN_BRANCH needs"):
        pool.cfg(PIN_BRANCH=ssd)
    with pytest.raises(ConfigError, match="PIN_FILE must be an absolute path"):
        pool.cfg(PIN_FILE="pins.json")
    with pytest.raises(ConfigError, match="SESSION_FILE must be an absolute path"):
        pool.cfg(SESSION_FILE="lock.json")
    cfg = pool.cfg()
    assert (cfg.pin_file, cfg.pin_branch, cfg.session_file) == ("", "", "")  # off by default
    ok = from_env({**pool.env, P + "PIN_FILE": "/p.json", P + "PIN_BRANCH": ssd + "/"})
    assert ok.pin_branch == ssd  # a trailing slash is ignored


# ------------------------------------------------------- C2: tiers.json
def test_tiers_json_has_the_documented_shape(pool, tmp_path):  # noqa: F811
    pool.add(pool.ssd, TITLE)
    pool.add(pool.hdd, "Away (1999)")
    season = pool.hdd / "tv/Show/Season 1"
    season.mkdir(parents=True)
    (season / "e1.mkv").write_bytes(b"y" * 100)
    pin_file = write_json(tmp_path / "pins.json", {"version": 1, "titles": ["Show"]})
    cfg = pool.cfg(DRY_RUN="false", PIN_FILE=pin_file, HEADROOM_GB="0")  # nothing needs to move
    before = time.time()
    mover.run(cfg, pool.jf(cfg), wait=False)
    doc = read_tiers(cfg)
    assert set(doc) == {"version", "updated", "units"}
    assert doc["version"] == 1
    assert isinstance(doc["updated"], int)
    assert before - 1 <= doc["updated"] <= time.time() + 1
    assert doc["units"] == [
        {"path": "movies/Away (1999)", "tier": "hdd", "bytes": 2048, "pinned": False},
        {"path": UNIT, "tier": "ssd", "bytes": 2048, "pinned": False},
        {"path": "tv/Show/Season 1", "tier": "hdd", "bytes": 100, "pinned": True},
    ]
    assert not os.path.exists(cfg.tiers_path + ".tmp")


def test_tiers_json_follows_a_move_and_a_dry_run_writes_it_too(pool):  # noqa: F811
    pool.add(pool.ssd, TITLE)
    dry = pool.cfg()
    mover.run(dry, pool.jf(dry))
    assert read_tiers(dry)["units"][0]["tier"] == "ssd"
    cfg = pool.cfg(DRY_RUN="false")
    mover.run_move(cfg, pool.jf(cfg), [TITLE], wait=False)
    assert read_tiers(cfg)["units"] == [{"path": UNIT, "tier": "hdd", "bytes": 2048, "pinned": False}]
    mover.run(cfg, pool.jf(cfg), wait=False)  # and a night's run rewrites it
    assert read_tiers(cfg)["version"] == 1


def test_tiers_build_marks_a_half_moved_title_as_ssd():
    u = core.Unit(UNIT, "movie", ssd_bytes=5, hdd_bytes=7)
    doc = tiers.build([u], [], ("movies",), 1234.9)
    assert doc == {
        "version": 1,
        "updated": 1234,
        "units": [{"path": UNIT, "tier": "ssd", "bytes": 12, "pinned": False}],
    }


def test_a_tiers_file_that_cannot_be_written_does_not_fail_the_run(pool):  # noqa: F811
    pool.add(pool.ssd, TITLE)
    pool.data.mkdir()
    os.mkdir(pool.data / "tiers.json")  # a folder where the file should go
    cfg = pool.cfg(DRY_RUN="false")
    result = mover.run(cfg, pool.jf(cfg), wait=False)
    assert result["note"] == "ok"


# ------------------------------------------------- C1 groundwork: the session lock
def session_doc(path, **kw):
    doc = {"version": 1, "owner": "shrink", "expires": time.time() + 600, **kw}
    return write_json(path, doc)


def test_session_check_states(tmp_path):
    now = 1_000_000.0
    path = tmp_path / "session.json"
    assert session.check("", now) == (session.CLOSED, "")  # no setting
    assert session.check(str(path), now) == (session.CLOSED, "")  # no file
    write_json(path, {"version": 1, "owner": "shrink", "expires": now + 5})
    assert session.check(str(path), now)[0] == session.OPEN
    write_json(path, {"version": 1, "owner": "shrink", "expires": now})  # expiry is the first closed second
    assert session.check(str(path), now)[0] == session.CLOSED
    state_, detail = session.check(str(path), now + 1)
    assert state_ == session.CLOSED
    assert "expired" in detail


@pytest.mark.parametrize(
    "content",
    [
        "",
        "{nope",
        "[]",
        '{"version": 1, "owner": "", "expires": 9999999999}',
        '{"version": 1, "owner": "  ", "expires": 9999999999}',
        '{"version": 1, "expires": 9999999999}',
        '{"version": 2, "owner": "x", "expires": 9999999999}',
        '{"version": 1, "owner": "x"}',
        '{"version": 1, "owner": "x", "expires": "soon"}',
        '{"version": 1, "owner": "x", "expires": true}',
    ],
)
def test_session_check_an_unreadable_file_is_not_closed(tmp_path, content):
    path = tmp_path / "session.json"
    path.write_text(content)
    assert session.check(str(path), 1.0)[0] == session.UNREADABLE


def test_an_unreadable_session_path_is_unreadable(tmp_path):
    path = tmp_path / "folder"
    path.mkdir()  # reading a folder as a file fails with an OS error
    assert session.check(str(path), 1.0)[0] == session.UNREADABLE


def test_nothing_moves_while_a_session_is_open(pool, tmp_path):  # noqa: F811
    f = pool.add(pool.ssd, TITLE)
    cfg = pool.cfg(DRY_RUN="false", SESSION_FILE=session_doc(tmp_path / "session.json"))
    ran = mover.run(cfg, pool.jf(cfg), wait=False)
    moved = mover.run_move(cfg, pool.jf(cfg), [TITLE], wait=False)
    for result in (ran, moved):
        assert "conversion session is open" in result["note"]
        assert "shrink" in result["note"]
        assert not result.get("failed")  # waiting for a session is normal, not an alert
        assert result["demoted"] == []
    assert f.exists()
    assert not os.path.exists(cfg.state_path)


def test_an_expired_session_does_not_hold_anything_back(pool, tmp_path):  # noqa: F811
    pool.add(pool.ssd, TITLE)
    cfg = pool.cfg(DRY_RUN="false", SESSION_FILE=session_doc(tmp_path / "session.json", expires=time.time() - 5))
    assert mover.run_move(cfg, pool.jf(cfg), [TITLE], wait=False)["demoted"]


def test_a_missing_session_file_does_not_hold_anything_back(pool, tmp_path):  # noqa: F811
    pool.add(pool.ssd, TITLE)
    cfg = pool.cfg(DRY_RUN="false", SESSION_FILE=str(tmp_path / "nothing-here.json"))
    assert mover.run(cfg, pool.jf(cfg), wait=False)["demoted"]


def test_an_unreadable_session_file_stands_down_and_says_why(pool, tmp_path, capsys):  # noqa: F811
    f = pool.add(pool.ssd, TITLE)
    path = tmp_path / "session.json"
    path.write_text("{garbled")
    cfg = pool.cfg(DRY_RUN="false", SESSION_FILE=str(path))
    for result in (mover.run(cfg, pool.jf(cfg), wait=False), mover.run_move(cfg, pool.jf(cfg), [TITLE], wait=False)):
        assert result["failed"]
        assert "conversion session file can't be trusted" in result["note"]
    assert f.exists()
    env = {**pool.env, P + "SESSION_FILE": str(path)}
    assert cli.main(["doctor"], env) == 1
    assert "[FAIL] conversion session file" in capsys.readouterr().out


def test_doctor_warns_about_an_open_session_and_passes_a_closed_one(pool, tmp_path, capsys):  # noqa: F811
    pool.add(pool.ssd, TITLE)
    path = tmp_path / "session.json"
    env = {**pool.env, P + "SESSION_FILE": session_doc(path)}
    assert cli.main(["doctor"], env) == 0
    assert "[warn] a conversion session is open" in capsys.readouterr().out
    path.unlink()
    assert cli.main(["doctor"], env) == 0
    assert "no session open" in capsys.readouterr().out


def test_a_session_that_opens_mid_run_stops_the_watcher_flag(pool, tmp_path):  # noqa: F811
    path = tmp_path / "session.json"
    cfg = pool.cfg(SESSION_FILE=str(path))
    watcher = mover.PlayingWatcher(cfg, pool.jf(cfg), interval=0.01)
    watcher.start()
    try:
        time.sleep(0.1)
        assert watcher.flag is False
        session_doc(path)
        deadline = time.time() + 5
        while not watcher.flag and time.time() < deadline:
            time.sleep(0.02)
        assert watcher.flag is True
        assert watcher.reason == "a conversion session opened"
    finally:
        watcher.stop()


def test_a_plan_is_read_only_while_a_session_is_open(pool, tmp_path, capsys):  # noqa: F811
    pool.add(pool.ssd, TITLE)
    env = {**pool.env, P + "SESSION_FILE": session_doc(tmp_path / "session.json")}
    assert cli.main(["plan"], env) == 0  # plan still shows what would happen; it never moves
    assert "Move to HDD (1)" in capsys.readouterr().out
