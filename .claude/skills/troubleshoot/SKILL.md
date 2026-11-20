---
name: troubleshoot
description: Diagnose LomboTier from doctor output and logs. Use when doctor fails, nothing moves, a move is refused, or the user asks why a title did or did not move.
---

# Troubleshoot LomboTier

Start with `lombo-tier doctor`, then `lombo-tier plan` (read the notes at the end). Never turn off
dry run or mounts checks to "see if it works".

| Symptom | Likely cause | Fix |
|---|---|---|
| Exit code 2, names a variable | Missing or bad setting | Fix that variable in `.env`. |
| Jellyfin unreachable | Wrong URL or network | Test the URL from inside the container. |
| API key refused | Wrong or revoked key | New key in Dashboard > API Keys. |
| Paths do not match the prefix | `JELLYFIN_PATH_PREFIX` wrong | Set it to where Jellyfin sees the pool root, for example `/media`. |
| Branch missing or not mounted | Disk down, wrong path, or not mounted into the container | Mount it at the same path as the setting. With `DISK_MARKER` set, that folder must exist in the branch. Nothing moves until fixed. |
| Not enough space on a branch | Destination needs room plus `DEST_MARGIN_GB` | Free space or lower the margin. |
| Library root does not exist | `MOVIE_ROOTS` or `SERIES_ROOTS` wrong | Use folders relative to the pool root. |
| Permission denied | Container user does not own the files | Set `user: "UID:GID"`. |
| "the SSDs have room" and nothing moves | SSD free space is above reserve + headroom | Normal. Nothing to fix. |
| A title never moves | Hard-linked to a seeding torrent, newer than `MIN_AGE_DAYS`, promoted within `PIN_DAYS`, or being watched | Check each; they are deliberate. |
| Over tonight's move limit | `MAX_GB_PER_RUN` reached | Raise it or wait. |
| Waiting for nobody to be watching | Someone is streaming | It waits up to `WAIT_QUIET_MINUTES`. |
| Plan never reaches the target with several SSDs | `RESERVE_GB` does not match mergerfs `minfreespace` | Make them equal. |

Confirm a claim in the code (`core.py` has the rules) or by running `doctor` and `plan` before
telling the user the cause. Do not run real moves to test.
