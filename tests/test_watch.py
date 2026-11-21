from datetime import UTC, datetime

import pytest

from lombo_tier import watch
from lombo_tier.jellyfin import Jellyfin, JellyfinError

P = "/media"


def ts(s):
    return datetime.fromisoformat(s).timestamp()


def item(path, **ud):
    d = {"Path": path}
    if ud:
        d["UserData"] = ud
    return d


def test_parse_ts():
    assert watch.parse_ts("2024-01-02T03:04:05Z") == datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC).timestamp()
    assert watch.parse_ts("2024-01-02T03:04:05.1234567Z") is not None
    for bad in (None, "", "nope", 5):
        assert watch.parse_ts(bad) is None


def test_build_watch_merges():
    created = [
        {"Path": "/media/movies/A/a.mkv", "DateCreated": "2024-01-01T00:00:00Z"},
        {"Path": "/elsewhere/b.mkv", "DateCreated": "2024-01-01T00:00:00Z"},
        {"DateCreated": "2024-01-01T00:00:00Z"},
    ]
    played = [
        item("/media/movies/A/a.mkv", PlayCount=2, LastPlayedDate="2024-02-01T00:00:00Z"),
        item("/elsewhere/b.mkv", PlayCount=1),
        {"UserData": {"PlayCount": 1}},
    ]
    resumable = [item("/media/movies/A/a.mkv", PlayCount=0, LastPlayedDate="2024-03-01T00:00:00Z")]
    other = [item("/media/movies/A/a.mkv", PlayCount=1, LastPlayedDate="2024-01-15T00:00:00Z")]
    w = watch.build_watch(created, [played, resumable, other], ["u1", "u1", "u2"], P)
    assert list(w) == ["movies/A/a.mkv"]
    a = w["movies/A/a.mkv"]
    assert a.created == ts("2024-01-01T00:00:00+00:00")
    assert a.plays == 3
    assert a.last_played == ts("2024-03-01T00:00:00+00:00")
    assert a.viewers == {"u1": ts("2024-03-01T00:00:00+00:00"), "u2": ts("2024-01-15T00:00:00+00:00")}


def test_playing_paths():
    s = [
        {"NowPlayingItem": {"Path": "/media/tv/S/s01/e.mkv"}},
        {"NowPlayingItem": {"Path": "/x/y.mkv"}},
        {"NowPlayingItem": {}},
        {},
    ]
    assert watch.playing_paths(s, P) == {"tv/S/s01/e.mkv"}
    assert watch.playing_paths(None, P) == set()


def test_paged_follows_pages(fake, monkeypatch):
    fake.items = [{"Path": f"/media/movies/m{i}/m.mkv"} for i in range(2500)]
    got = Jellyfin(fake.url, "good-key").paged("/Items")
    assert len(got) == 2500
    assert len([r for r in fake.requests if r["path"].startswith("/Items")]) == 3
    monkeypatch.setattr(Jellyfin, "page_size", 7)
    fake.requests.clear()
    assert len(Jellyfin(fake.url, "good-key").paged("/Items")) == 2500
    assert fake.requests[1]["query"]["StartIndex"] == "7"


def test_query_encoding(fake):
    Jellyfin(fake.url, "good-key")._get("/Items", {"Fields": "Path,DateCreated", "q": "a b&c"})
    q = fake.requests[-1]["query"]
    assert q == {"Fields": "Path,DateCreated", "q": "a b&c"}
    assert "&c" not in fake.requests[-1]["path"]


def test_wrong_key(fake):
    with pytest.raises(JellyfinError):
        Jellyfin(fake.url, "bad").users()


def test_collect_watch_end_to_end(fake):
    a, b = "/media/movies/A/a.mkv", "/media/tv/S/s01/e1.mkv"
    fake.users = [{"Id": "u1"}, {"Id": "u2"}]
    fake.items = [
        {"Path": a, "DateCreated": "2024-01-01T00:00:00Z"},
        {"Path": b, "DateCreated": "2024-01-02T00:00:00Z"},
    ]
    fake.user_items = {
        "u1": {
            "IsPlayed": [item(a, PlayCount=1, LastPlayedDate="2024-02-01T00:00:00Z")],
            "IsResumable": [item(b, PlayCount=0, LastPlayedDate="2024-02-05T00:00:00Z")],
        },
        "u2": {"IsPlayed": [item(a, PlayCount=2, LastPlayedDate="2024-02-03T00:00:00Z")]},
    }
    w = watch.collect_watch(Jellyfin(fake.url, "good-key"), P)
    assert w["movies/A/a.mkv"].plays == 3
    assert set(w["movies/A/a.mkv"].viewers) == {"u1", "u2"}
    assert w["tv/S/s01/e1.mkv"].viewers.keys() == {"u1"}
    assert w["tv/S/s01/e1.mkv"].created == ts("2024-01-02T00:00:00+00:00")
    flt = [r["query"].get("Filters") for r in fake.requests if r["path"].startswith("/Users/u")]
    assert flt.count("IsPlayed") == 2
    assert flt.count("IsResumable") == 2


def test_current_playing(fake):
    fake.sessions = [{"NowPlayingItem": {"Path": "/media/movies/A/a.mkv"}}]
    assert watch.current_playing(Jellyfin(fake.url, "good-key"), P) == {"movies/A/a.mkv"}


def test_a_played_item_that_is_also_resumable_counts_once_per_person():
    both = item("/media/movies/A/a.mkv", PlayCount=1, LastPlayedDate="2024-02-01T00:00:00Z")
    w = watch.build_watch([], [[both], [both]], ["u1", "u1"], P)
    assert w["movies/A/a.mkv"].plays == 1
    w = watch.build_watch([], [[both], [both]], ["u1", "u2"], P)  # two people: still two plays
    assert w["movies/A/a.mkv"].plays == 2
