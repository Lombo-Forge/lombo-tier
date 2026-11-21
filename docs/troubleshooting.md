# Troubleshooting

## Contents

- [First steps](#first-steps)
- [Messages and fixes](#messages-and-fixes)
- [Nothing moves](#nothing-moves)
- [Exit codes](#exit-codes)

## First steps

Run `lombo-tier doctor`. It checks each setting and each disk and says which one failed. Then run `lombo-tier plan`. The notes at the end of the plan explain most "why did it not move X" questions.

## Messages and fixes

Wording may differ slightly from the messages in your version. Match on the meaning.

| What you see | Cause | Fix |
|---|---|---|
| Bad setting, exit code 2 | A required variable is missing or has a bad value | Fix the variable it names in `.env`. |
| Jellyfin unreachable | Wrong URL, or not on the same network | Check `LOMBO_TIER_JELLYFIN_URL` from inside the container. |
| API key refused | Wrong or revoked key | Make a new key in Dashboard > API Keys. |
| Jellyfin paths do not match the prefix | `JELLYFIN_PATH_PREFIX` is not where Jellyfin sees the pool root | Set it to the folder above `movies` and `tv` as Jellyfin reports it, for example `/media`. |
| A branch is missing or not mounted | Disk not mounted, wrong path, or the container does not see it | Mount the disk and check the compose mount uses the same path as the setting. With `DISK_MARKER` set, a folder of that name must exist in the branch. Nothing moves until it is fixed. |
| A branch has no space | Destination lacks room plus `DEST_MARGIN_GB` | Free space or lower the margin. |
| `[warn] library folder tv: not found on any disk` | You have no TV (or no movies) folder, or the name is wrong | Fine if it is true. Otherwise fix `MOVIE_ROOTS` or `SERIES_ROOTS`: folders relative to the pool root, comma-separated. |
| `[FAIL] library folders: none of ... exist on any disk` | Neither `MOVIE_ROOTS` nor `SERIES_ROOTS` matches a folder | Fix both. They are relative to the pool root, for example `movies` and `tv`. |
| `[FAIL] path prefix: Jellyfin reports ...` | `JELLYFIN_PATH_PREFIX` does not match how Jellyfin names the files | Use the value `doctor` suggests in the same line. |
| Permission denied while copying | Container user does not own the files | Set `user: "UID:GID"` to the owner of the media. |
| Skipped: over tonight's move limit | Title is larger than what is left of `MAX_GB_PER_RUN` | Raise `MAX_GB_PER_RUN` or wait for the next run. |
| Wanted on the SSD but there isn't room even after moving older titles | Not enough older titles are old enough to move | Wait, or lower `MIN_AGE_DAYS`, or add SSD space. |
| Still short of the target after moving everything old enough | Everything left is too new, watched, held or recently promoted | Wait, or review the settings. |
| `the pin list can't be read` (and `[FAIL] pin list` in `doctor`) | The pin file exists but is not valid JSON in the documented shape | Fix it, or move it aside. Until then nothing moves, on purpose. See [pinned-titles.md](pinned-titles.md). |
| `a conversion session is open` | The file at `SESSION_FILE` says another tool is converting | Nothing to fix: LomboTier tries again next run. If no tool is converting, the file is stale: it stops counting at its `expires` time, or delete it. |
| `the conversion session file can't be trusted` | The session file exists but is not valid | Fix or remove it. An unreadable lock is never treated as closed. See [contracts.md](contracts.md). |
| `another run or move is in progress` | A second run or `move` started while one was going | Wait for the first. The lock is released when it ends, even if it crashed. |
| `the pin branch ... is missing or not mounted` | `PIN_BRANCH` is not there | Mount it. Pinned titles do not move meanwhile. |
| `pinned: never moved off the SSD` | `move` was given a pinned title | `lombo-tier pin remove NAME` first, if you do want it moved. |
| Waiting for nobody to be watching | Someone is streaming | It waits up to `WAIT_QUIET_MINUTES`, then stops without moving. Use `--no-wait` only if you accept that. |

## Nothing moves

- **The SSD has room.** The plan says "the SSDs have room". Titles only go to the HDDs when SSD free space is below `RESERVE_GB` plus `HEADROOM_GB`. This is normal.
- **Hard-linked files.** A file with another hard link, usually a torrent that is still seeding, keeps its whole title in place. Stop seeding or remove the link.
- **Too new.** Titles newer than `MIN_AGE_DAYS` stay.
- **Recently promoted.** A title that came back to the SSD stays for `PIN_DAYS`.
- **Dry run.** `DRY_RUN=true` only prints.
- **The wrong free space.** If you have several SSDs, `RESERVE_GB` should match mergerfs `minfreespace`. See [how it works](how-it-works.md#counting-free-space).
- **Stale HDD list.** The HDD file list is refreshed after `HDD_INDEX_MAX_DAYS`. A title added to an HDD directly may not show until then.

## Exit codes

0 ok, 1 a failure (or a `move` that did not start because another run holds the lock), 2 bad settings (or a title
name that `move` or `pin` cannot find or that matches several).
