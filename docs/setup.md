# Setup

## Contents

- [What you need](#what-you-need)
- [1. The pool](#1-the-pool)
- [2. Jellyfin key and paths](#2-jellyfin-key-and-paths)
- [3. Configure](#3-configure)
- [4. Start it](#4-start-it)
- [5. Try it safely](#5-try-it-safely)
- [6. Go live](#6-go-live)
- [Running once a night with systemd](#running-once-a-night-with-systemd)

## What you need

- A mergerfs pool whose branches are separate disks, SSDs first in the branch order.
- Jellyfin and an API key.
- Docker. The container needs read-write access to each branch folder, at the same paths as in your settings, and must run as the user that owns the media files.

## 1. The pool

Use the branch folders of the individual disks, not the pool mount. List the SSDs first. Example (see [examples/mergerfs-fstab.example](../examples/mergerfs-fstab.example)):

```
/mnt/ssd1:/mnt/hdd1:/mnt/hdd2 /media fuse.mergerfs cache.files=off,category.create=ff,func.getattr=newest,dropcacheonclose=false,moveonenospc=true,minfreespace=40G,fsname=mediapool,nofail 0 0
```

- `category.create=ff` sends a new file to the first branch with enough free space, which is the SSD.
- `minfreespace=40G` is where new files stop landing on a branch. Make it equal to `LOMBO_TIER_RESERVE_GB`.
- `moveonenospc=true` lets a write move to another branch when one fills up.

These options come from a setup that has been running for a while, but mergerfs options differ by version. Check yours in the mergerfs documentation. After editing fstab, remount the pool.

## 2. Jellyfin key and paths

1. Dashboard > API Keys > +, name it LomboTier.
2. Note where Jellyfin sees the pool root, for example `/media`. That is `JELLYFIN_PATH_PREFIX`. A movie at `/media/movies/Jaws (1975)/Jaws.mkv` is `movies/Jaws (1975)/Jaws.mkv` in the pool.

## 3. Configure

Copy `.env.example` to `.env`. Set at least:

- `LOMBO_TIER_JELLYFIN_URL`, `LOMBO_TIER_JELLYFIN_API_KEY`, `LOMBO_TIER_JELLYFIN_PATH_PREFIX`
- `LOMBO_TIER_SSD_BRANCHES` and `LOMBO_TIER_HDD_BRANCHES`, comma-separated, the folders of the individual disks
- `LOMBO_TIER_MOVIE_ROOTS` and `LOMBO_TIER_SERIES_ROOTS` if your folders are not `movies` and `tv`

Leave `LOMBO_TIER_DRY_RUN=true`.

In `compose.example.yaml`, mount each branch at the same path inside the container as on the host, and set `user: "UID:GID"` to the owner of your media files (`id -u` and `id -g` for that user, or `stat -c '%u:%g' /mnt/hdd1/movies`).

## 4. Start it

```sh
docker compose -f compose.example.yaml up -d
docker compose -f compose.example.yaml logs -f lombo-tier
```

## 5. Try it safely

Never test on a pool you cannot afford to touch until you have done these in order, with `DRY_RUN=true`:

1. `lombo-tier doctor`. Fix everything it reports. See [troubleshooting](troubleshooting.md).
2. `lombo-tier plan`. Read it. Do the titles and reasons make sense?
3. `lombo-tier move "Some Small Movie (2001)"`. With `DRY_RUN=true` this only prints what it would do. Pick a small title.

## 6. Go live

1. Set `LOMBO_TIER_DRY_RUN=false`, restart the container.
2. Run `lombo-tier move "Some Small Movie (2001)"` once. Check the files are on an HDD branch and that Jellyfin still plays it.
3. Leave `run --loop` going. It runs daily at `LOMBO_TIER_RUN_HOUR`, in the timezone set by `TZ`.

## Running once a night with systemd

Instead of `--loop`, a timer can start a container each night. Edit the paths in [examples/lombo-tier.service](../examples/lombo-tier.service), then copy it and [examples/lombo-tier.timer](../examples/lombo-tier.timer) to `/etc/systemd/system/` and run:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now lombo-tier.timer
```

The container then runs `run` once. The timer's time is the run time; `RUN_HOUR` is only used by `--loop`.
