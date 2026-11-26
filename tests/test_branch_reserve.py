"""One SSD branch can keep more free than the others (a boot disk that also holds the system)."""

import pytest

from lombo_tier import core, disks
from lombo_tier.config import ConfigError, _branch_reserve

GB = core.GB


def test_parsing_and_refusals():
    assert _branch_reserve("", ("/a",)) == ()
    assert _branch_reserve("/a/=250, /b=0", ("/a", "/b")) == (("/a", 250 * GB), ("/b", 0))
    for bad in ("/c=5", "/a", "/a=x", "/a=-1", "/a=inf", "/a=nan"):
        with pytest.raises(ConfigError, match="BRANCH_RESERVE_GB"):
            _branch_reserve(bad, ("/a", "/b"))


def test_only_room_above_a_branchs_own_reserve_counts(monkeypatch, tmp_path):
    boot, other = str(tmp_path / "boot"), str(tmp_path / "ssd")
    for d in (boot, other):
        (tmp_path / d).mkdir()
    free = {boot: 300 * GB, other: 100 * GB}
    monkeypatch.setattr(disks, "disk_free", free.__getitem__)
    ((a, _),) = disks.branch_free([boot], {boot: 210 * GB})
    assert a == 90 * GB  # 300 free, keeps 250: like 90 on a branch that keeps 40
    assert disks.branch_free([boot], {boot: 400 * GB})[0][0] == 0


def test_a_file_never_lands_below_a_branchs_own_reserve(monkeypatch):
    free = {"/boot": 300 * GB, "/ssd": 100 * GB}
    monkeypatch.setattr(disks, "disk_free", free.__getitem__)
    assert disks.pick_branch(["/boot", "/ssd"], 60 * GB) == "/boot"
    assert disks.pick_branch(["/boot", "/ssd"], 60 * GB, reserves={"/boot": 250 * GB}) == "/ssd"
    assert disks.pick_branch(["/boot"], 60 * GB, reserves={"/boot": 250 * GB}) is None
