<p align="center"><img src="assets/banner.png" alt="LomboTier" width="100%"></p>

<p align="center">
  <a href="LICENSE"><img alt="License: GPL-3.0" src="https://img.shields.io/github/license/Lombo-Forge/lombo-tier"></a>
  <a href="https://github.com/Lombo-Forge/lombo-tier/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/Lombo-Forge/lombo-tier/ci.yml?branch=main&label=CI"></a>
  <a href="https://github.com/Lombo-Forge/lombo-tier/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://img.shields.io/github/actions/workflow/status/Lombo-Forge/lombo-tier/codeql.yml?branch=main&label=CodeQL"></a>
  <a href="https://github.com/Lombo-Forge/lombo-tier/releases"><img alt="Release" src="https://img.shields.io/github/v/release/Lombo-Forge/lombo-tier?include_prereleases"></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-blue">
  <a href="https://ko-fi.com/datlombardo"><img alt="Support on Ko-fi" src="https://img.shields.io/badge/Ko--fi-support-FF5E5B?logo=ko-fi&logoColor=white"></a>
</p>

# LomboTier

LomboTier keeps what people watch on your SSD and moves the rest to your HDDs. It moves whole movie folders and TV season folders inside a mergerfs pool, based on what Jellyfin says was played.

## Contents

- [What `plan` shows](#what-plan-shows)
- [Install in a minute](#install-in-a-minute)
- [How it decides](#how-it-decides)
- [Commands](#commands)
- [Settings](#settings)
- [FAQ](#faq)
- [Not included](#not-included)
- [Contributing and licence](#contributing-and-licence)

## What `plan` shows

Made-up numbers and titles, real format. `plan` only shows; it never moves anything:

```
SSD free (counted for planning): 52.0 GB; target 80.0 GB
Move to HDD (2):
  movies/Old Western (1962)  18.4 GB  never watched, added 61 days ago
  tv/Quiet Show/Season 02  41.0 GB  last watched 94 days ago
Move back to SSD (1):
  tv/Space Show/Season 03  36.2 GB  next season of 3 episodes watched this week
Most watched recently: tv/Space Show/Season 02 (3 viewers, on ssd)
```

## Install in a minute

1. In Jellyfin, open Dashboard > API Keys and create a key for LomboTier.
2. Copy `.env.example` to `.env` and fill in the Jellyfin URL, the key, the path prefix and your branch folders.
3. Start it and check it:
   ```sh
   docker compose -f compose.example.yaml up -d
   docker compose -f compose.example.yaml exec lombo-tier lombo-tier doctor
   docker compose -f compose.example.yaml exec lombo-tier lombo-tier plan
   ```

It starts in dry-run mode and moves nothing. Try `doctor`, then `plan`, then a single `move` with `LOMBO_TIER_DRY_RUN=true`. Set it to `false` only after that. The full steps are in [docs/setup.md](docs/setup.md).

## How it decides

- It moves a title to the HDDs only when SSD free space is below the reserve plus the headroom. Coldest first: never watched, then least popular, then longest unwatched.
- It never moves a title that is playing, is still hard-linked to a seeding torrent, is newer than `MIN_AGE_DAYS`, or came back to the SSD within `PIN_DAYS`.
- It moves titles that are being watched back to the SSD. For a series, several episodes this week bring over the season in progress and the next one. A movie played `HOT_PLAYS` times this month comes over. Anything that `POPULAR_VIEWERS` different people watched this month comes over.
- The unit is a movie folder or one season folder, and a run moves at most `MAX_GB_PER_RUN`.
- Every copy is checked by hash before the original is removed. A move stops when someone starts watching. If a pool disk is not mounted, nothing moves.

More detail: [docs/how-it-works.md](docs/how-it-works.md).

## Commands

| Command | What it does |
|---|---|
| `lombo-tier doctor` | Checks settings, Jellyfin, the API key, that the SSD and HDD branches exist, are mounted and have space, that at least one of your library folders exists, and that Jellyfin's file paths match the prefix. Exit code 1 on a failure. |
| `lombo-tier plan [--json]` | Shows tonight's plan. Changes nothing. Does not wake a sleeping HDD beyond what the saved file list needs. |
| `lombo-tier run [--no-wait] [--loop]` | Does the plan. With `DRY_RUN=true` it only prints it. It waits up to `WAIT_QUIET_MINUTES` for nobody to be watching (`--no-wait` skips that). `--loop` repeats daily at `RUN_HOUR` in the container's `TZ`. |
| `lombo-tier move NAME [NAME...]` | Moves the named titles to the HDDs now, with the same safety checks. `NAME` is a title folder such as `Jaws (1975)` or a pool path such as `movies/Jaws (1975)`. |

Exit codes: 0 ok, 1 failure, 2 bad settings (or a title name that `move` cannot find).

## Settings

Everything is an environment variable with the `LOMBO_TIER_` prefix. All of them, with defaults, are in [.env.example](.env.example).

## FAQ

**Is it safe?** It is safe by default: `LOMBO_TIER_DRY_RUN=true` until you set it to false. It copies, checks the copy by hash, and only then removes the original. It still moves real files, so try it as described in [docs/setup.md](docs/setup.md) and keep backups of anything you cannot replace.

**What if a disk is missing?** Nothing moves. With `CHECK_MOUNTS=true` it checks every branch is mounted before it starts, and `doctor` tells you which one is not.

**Does it need Sonarr or Radarr?** No. It reads watch data from Jellyfin and works on folders. Paths in the pool do not change when a title moves, so those apps do not notice.

**What about torrents that are still seeding?** A title with a file that has another hard link is left where it is. When the torrent is gone, the title can move.

## Not included

- Pinning a title to one disk.
- A web interface.
- Sonarr or Radarr integration.
- A reserve for a boot disk.

## Contributing and licence

Contribution, security and conduct rules come from the [Lombo-Forge organisation](https://github.com/Lombo-Forge/.github). Licensed under [GPL-3.0](LICENSE). Credits are in [NOTICE.md](NOTICE.md). Not affiliated with or endorsed by the Jellyfin project.
