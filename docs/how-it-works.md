# How it works

## Contents

- [The setup it expects](#the-setup-it-expects)
- [What moves](#what-moves)
- [Moving to the HDDs](#moving-to-the-hdds)
- [Moving to the SSD](#moving-to-the-ssd)
- [How a move is done safely](#how-a-move-is-done-safely)
- [Counting free space](#counting-free-space)
- [A run, step by step](#a-run-step-by-step)

## The setup it expects

Your media is in one mergerfs pool made of separate disks. The SSDs come first in the branch order, so new files land on an SSD until its free-space floor is reached. After that they land on an HDD. LomboTier does not control where new files go. It only moves existing ones, between branches, so the path in the pool stays the same. Jellyfin sees the pool. LomboTier sees the branch folders.

## What moves

The unit is a movie folder, or one season folder of a show. A title is never split by this tool. Loose files at a show's top folder or in a library root stay where they are. Season folders are recognised by name: `Season 1`, `Series 2`, `S03`, and `Specials` (season 0).

## Moving to the HDDs

This only happens when SSD free space is below `RESERVE_GB` plus `HEADROOM_GB`. It stops when the SSD has that much free again.

Candidates are ordered coldest first:

1. Never watched.
2. Least popular. Each person who watched counts once, worth half as much for every `HALF_LIFE_DAYS` since they last watched.
3. Longest unwatched.

A title is skipped if it is playing now, is hard-linked to something else (a seeding torrent), is newer than `MIN_AGE_DAYS`, came back to the SSD within `PIN_DAYS`, or is currently counted as being watched.

## Moving to the SSD

A title is being watched when:

- Series: at least `PROMOTE_EPISODES` different episodes were watched in the last 7 days, or an episode is playing. The season in progress and the next season come over.
- Movie: played `HOT_PLAYS` times in the last 30 days.
- Any title: `POPULAR_VIEWERS` different people watched it in the last 30 days. For a series, the next season comes over too.

The most popular come first. A title only moves up if the SSD keeps its reserve afterwards, counting what could be moved down to make room. Otherwise the plan says so in a note.

## How a move is done safely

- Files are copied to the other branch and checked by hash. The original is removed only after the copy matches.
- A move stops the moment someone starts watching.
- If a pool disk is not mounted, nothing moves.
- One run moves at most `MAX_GB_PER_RUN` and runs at most `MAX_RUN_MINUTES`. The destination must keep `DEST_MARGIN_GB` free.
- Before the run starts it waits up to `WAIT_QUIET_MINUTES` for nobody to be watching.
- A title that was moved up is not moved down for `PIN_DAYS`. The cooldowns are kept in `state.json`.

## Counting free space

mergerfs stops sending new files to a branch once it has less than `minfreespace` free. So the last `minfreespace` of each SSD is not room for new files. LomboTier counts, for each SSD, only the free space above `RESERVE_GB`, adds one reserve, and counts disks on the same filesystem once. A plain sum over several SSDs would stay above the target forever and nothing would ever move. Set mergerfs `minfreespace` equal to `RESERVE_GB`.

## A run, step by step

1. Check that `state.json` is readable and, with `CHECK_MOUNTS=true`, that every branch is mounted. If not, stop: nothing moves.
2. Read what Jellyfin knows about plays, per user, and who is watching now.
3. Scan the SSD branches. The HDD file list is kept in `hdd-index.json` in `DATA_DIR` and refreshed when it is older than `HDD_INDEX_MAX_DAYS`, so sleeping HDDs are not woken every night.
4. Build the plan.
5. If `DRY_RUN=true`, print it and stop. A dry run does not write `state.json`.
6. If there is something to move, walk the HDDs fresh (they are about to wake anyway) and plan again, so the plan matches the disks.
7. Wait up to `WAIT_QUIET_MINUTES` for nobody to be watching. If people keep watching, stop without moving anything.
8. Do the moves, checking Jellyfin every 30 seconds. If someone starts watching, the current file stops cleanly and the rest are skipped.
9. Walk the HDDs once more so the saved file list matches, then write `state.json` (last run and a cooldown per moved title).
10. Send the webhook summary if something moved or failed, and ping `PING_URL`.
