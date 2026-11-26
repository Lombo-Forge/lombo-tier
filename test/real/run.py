"""LomboTier on a THROWAWAY setup: a real Jellyfin and real files. Never point it at your own.

Needs Linux, docker and /dev/shm. The "SSD" branch is in /dev/shm and the "HDD" branch on the disk, so every move
crosses two real filesystems. A pool path is "that path on any branch", which is what mergerfs shows; mergerfs itself
is NOT mounted (that needs root), so how a real pool behaves during a move is not covered here.
Run from the repository: python3 test/real/run.py
"""

# ruff: noqa: S310, S603, S607  (fixed local address; docker and this app only)

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE = "http://127.0.0.1:18196"
IMAGE = os.environ.get("JELLYFIN_IMAGE", "jellyfin/jellyfin:12.4")
AUTH = 'MediaBrowser Client="lf", Device="lf", DeviceId="{0}", Version="1"'
failures: list[str] = []


def check(name: str, ok: object, detail: object = "") -> None:
    print(("[ ok ] " if ok else "[FAIL] ") + name + ("" if ok or detail == "" else f"  -> {str(detail)[:600]}"))
    if not ok:
        failures.append(name)


def call(method: str, path: str, body: Any = None, token: str = "", device: str = "lf") -> Any:
    auth = AUTH.format(device) + (f', Token="{token}"' if token else "")
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path, data, {"Content-Type": "application/json", "Authorization": auth}, method=method
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode()
    return json.loads(text) if text.startswith(("{", "[")) else text


def digest(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def main() -> None:
    work = os.path.join(ROOT, ".scratch-jf")
    shutil.rmtree(work, ignore_errors=True)
    ssd = tempfile.mkdtemp(prefix="lf-tier-", dir="/dev/shm")
    hdd, data = (os.path.join(work, n) for n in ("hdd", "data"))
    for d in (hdd, data, f"{work}/config", f"{work}/cache", f"{ssd}/.disk", f"{hdd}/.disk"):
        os.makedirs(d, exist_ok=True)
    try:
        scenario(work, ssd, hdd, data)
    finally:
        subprocess.run(["docker", "rm", "-f", "lf-tier-jf"], capture_output=True)
        shutil.rmtree(ssd, ignore_errors=True)
    print(f"\n{len(failures)} failed" if failures else "\nall passed")
    sys.exit(1 if failures else 0)


def scenario(work: str, ssd: str, hdd: str, data: str) -> None:
    def pooled(rel: str) -> str:
        found = [f"{b}/{rel}" for b in (ssd, hdd) if os.path.isfile(f"{b}/{rel}")]
        return found[0] if len(found) == 1 else f"({len(found)} copies of {rel})"

    user = ["--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp"]
    movies = ["Cold (2001)", "Watched (2002)", "Playing (2003)", "Seeding (2004)", "Pinned (2005)"]
    files = [f"movies/{m}/{m}.mkv" for m in movies]
    files += [f"tv/Show/Season 0{s}/Show S0{s}E01.mkv" for s in (1, 2)]
    for rel in files:
        os.makedirs(os.path.dirname(f"{ssd}/{rel}"), exist_ok=True)
        cmd = ["docker", "run", "--rm", *user, "-v", f"{ssd}:/m", "--entrypoint", "/usr/lib/jellyfin-ffmpeg/ffmpeg"]
        cmd += [IMAGE, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=20"]
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-metadata", f"title={rel}", f"/m/{rel}"]
        subprocess.run(cmd, check=True)
    old = time.time() - 90 * 86400
    for rel in files:
        os.utime(f"{ssd}/{rel}", (old, old))
    os.link(f"{ssd}/{files[3]}", f"{ssd}/seeding.link")  # a torrent still holding the file
    hashes = {rel: digest(f"{ssd}/{rel}") for rel in files}

    run = ["docker", "run", "-d", "--name", "lf-tier-jf", "--cpus", "2", "--memory", "2g", *user]
    run += ["-p", "127.0.0.1:18196:8096", "-v", f"{work}/config:/config", "-v", f"{work}/cache:/cache"]
    subprocess.run([*run, "-v", f"{ssd}:/media:ro", IMAGE], check=True, capture_output=True)
    for _ in range(90):
        try:
            call("GET", "/Startup/Configuration")
            break
        except OSError:
            time.sleep(2)
    lang = {"UICulture": "en-US", "MetadataCountryCode": "US", "PreferredMetadataLanguage": "en"}
    call("POST", "/Startup/Configuration", lang)
    call("GET", "/Startup/User")
    call("POST", "/Startup/User", {"Name": "admin", "Password": "lf-test-pass"})
    call("POST", "/Startup/Complete")
    me = call("POST", "/Users/AuthenticateByName", {"Username": "admin", "Pw": "lf-test-pass"})
    token, uid = me["AccessToken"], me["User"]["Id"]
    for name, kind, folder in (("Movies", "movies", "movies"), ("Shows", "tvshows", "tv")):
        opts = {"LibraryOptions": {"PathInfos": [{"Path": f"/media/{folder}"}]}}
        call("POST", f"/Library/VirtualFolders?name={name}&collectionType={kind}&refreshLibrary=true", opts, token)
    items: list[dict[str, Any]] = []
    for attempt in range(60):
        time.sleep(2)
        q = "/Items?Recursive=true&IncludeItemTypes=Movie,Episode&Fields=Path"
        items = call("GET", q, token=token)["Items"]
        if len(items) >= len(files):
            break
        if attempt in (8, 20):
            call("POST", "/Library/Refresh", token=token)
    check("Jellyfin sees every file", len(items) == len(files), [i.get("Path") for i in items])
    ids = {i["Path"].removeprefix("/media/"): i["Id"] for i in items}
    call("POST", "/Auth/Keys?app=lf-tier", token=token)
    key = call("GET", "/Auth/Keys", token=token)["Items"][0]["AccessToken"]
    call("POST", f"/UserPlayedItems/{ids[files[1]]}?userId={uid}", token=token)
    playing = {"ItemId": ids[files[2]], "PositionTicks": 0, "CanSeek": True}
    viewer = call("POST", "/Users/AuthenticateByName", {"Username": "admin", "Pw": "lf-test-pass"}, device="tv")
    call("POST", "/Sessions/Playing", playing, viewer["AccessToken"], "tv")

    with open(f"{data}/pins.json", "w") as f:
        json.dump({"version": 1, "titles": ["movies/Pinned (2005)"]}, f)
    env = {
        **os.environ,
        "PYTHONPATH": f"{ROOT}/src",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LOMBO_TIER_JELLYFIN_URL": BASE,
        "LOMBO_TIER_JELLYFIN_API_KEY": key,
        "LOMBO_TIER_JELLYFIN_PATH_PREFIX": "/media",
        "LOMBO_TIER_SSD_BRANCHES": ssd,
        "LOMBO_TIER_HDD_BRANCHES": hdd,
        "LOMBO_TIER_DISK_MARKER": ".disk",
        "LOMBO_TIER_DATA_DIR": data,
        "LOMBO_TIER_PIN_FILE": f"{data}/pins.json",
        "LOMBO_TIER_SESSION_FILE": f"{data}/session.json",
        "LOMBO_TIER_RESERVE_GB": "100000",  # never reachable: everything that may move, moves
        "LOMBO_TIER_HEADROOM_GB": "0",
        "LOMBO_TIER_DEST_MARGIN_GB": "0",
        "LOMBO_TIER_WAIT_QUIET_MINUTES": "0",
    }

    def tier(*args: str, live: bool = True) -> tuple[int, str]:
        e = {**env, "LOMBO_TIER_DRY_RUN": "false" if live else "true"}
        r = subprocess.run([sys.executable, "-m", "lombo_tier.cli", *args], env=e, capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr

    def on_ssd() -> set[str]:
        return {rel for rel in files if os.path.exists(f"{ssd}/{rel}")}

    code, out = tier("doctor")
    check("doctor passes", code == 0, out)
    code, out = tier("plan")
    print("       " + out.strip().replace("\n", "\n       "))
    check("plan changes nothing", on_ssd() == set(files))
    tier("run", "--no-wait", live=False)
    check("a dry run moves nothing", on_ssd() == set(files))

    with open(f"{data}/session.json", "w") as f:
        json.dump({"opened": time.time(), "expires": time.time() + 600}, f)  # a lock written by some other tool
    code, out = tier("run", "--no-wait")
    check("an unknown session file shape: stand down", on_ssd() == set(files), out)
    os.remove(f"{data}/session.json")
    os.rmdir(f"{hdd}/.disk")
    code, out = tier("run", "--no-wait")
    check("a disk not mounted: nothing moves", on_ssd() == set(files) and code != 0, out)
    os.mkdir(f"{hdd}/.disk")

    held = open(f"{ssd}/{files[0]}", "rb")  # noqa: SIM115  # a reader that stays open across the move
    code, out = tier("run", "--no-wait")
    print("       " + out.strip().replace("\n", "\n       "))
    left = on_ssd()
    check("the cold movie moved to the HDD", files[0] not in left and os.path.isfile(f"{hdd}/{files[0]}"), out)
    check("the playing movie stayed", files[2] in left, out)
    check("the hard-linked (seeding) movie stayed", files[3] in left, out)
    check("the pinned movie stayed", files[4] in left, out)
    same = [digest(pooled(r)) == h for r, h in hashes.items()]
    check("every file is on one branch only, same path, same bytes", all(same))
    check("a file opened before the move still reads", hashlib.sha256(held.read()).hexdigest() == hashes[files[0]])
    held.close()
    check("the moved file kept its date", abs(os.path.getmtime(pooled(files[0])) - old) < 2)
    with open(f"{data}/tiers.json") as f:
        tiers = {u["path"]: u["tier"] for u in json.load(f)["units"]}
    check("tiers.json says hdd for it", tiers.get("movies/Cold (2001)") == "hdd", tiers)
    strays = [n for d in (ssd, hdd) for _, _, names in os.walk(d) for n in names if n.endswith((".part", ".tmp"))]
    check("no half-copied files left", not strays, strays)

    for _ in range(2):  # watched twice: it should come back to the SSD
        call("DELETE", f"/UserPlayedItems/{ids[files[0]]}?userId={uid}", token=token)
        call("POST", f"/UserPlayedItems/{ids[files[0]]}?userId={uid}", token=token)
    info = f"/Items/{ids[files[0]]}?userId={uid}"
    print(f"       plays Jellyfin reports: {call('GET', info, token=token)['UserData'].get('PlayCount')}")
    env["LOMBO_TIER_RESERVE_GB"] = "0"
    env["LOMBO_TIER_HOT_PLAYS"] = "1"
    code, out = tier("run", "--no-wait")
    print("       " + out.strip().replace("\n", "\n       "))
    back = files[0] in on_ssd() and digest(pooled(files[0])) == hashes[files[0]]
    check("a watched movie comes back to the SSD", back)


main()
