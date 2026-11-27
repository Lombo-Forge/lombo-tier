"""A disk's top folder may belong to root: what matters is the library folders moves write into."""

import os

from lombo_tier.cli import _unwritable


def test_only_the_folders_moves_write_into_must_be_writable(tmp_path, monkeypatch):
    (tmp_path / "movies").mkdir()
    locked = {str(tmp_path)}
    monkeypatch.setattr(os, "access", lambda path, mode: str(path) not in locked)
    assert _unwritable(str(tmp_path), ["movies", "tv"]) is None
    locked.add(str(tmp_path / "movies"))
    assert _unwritable(str(tmp_path), ["movies", "tv"]) == str(tmp_path / "movies")
    locked.remove(str(tmp_path / "movies"))
    assert _unwritable(str(tmp_path), ["tv"]) == str(tmp_path)  # no library folder yet: the disk itself must be
