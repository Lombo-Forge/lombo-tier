# How it works

## Contents

- [The setup it expects](#the-setup-it-expects)
- [What moves](#what-moves)
- [Moving to the HDDs](#moving-to-the-hdds)
- [Moving to the SSD](#moving-to-the-ssd)
- [How a move is done safely](#how-a-move-is-done-safely)
- [Counting free space](#counting-free-space)
- [Moving one title now](#moving-one-title-now)
- [A run, step by step](#a-run-step-by-step)

## The setup it expects

Your media is in one mergerfs pool made of separate disks. The SSDs come first in the branch order, so new files land on an SSD until its free-space floor is reached. After that they land on an HDD. LomboTier does not control where new files go. It only moves existing ones, between branches, so the path in the pool stays the same. Jellyfin sees the pool. LomboTier sees the branch folders.

## What moves

The unit is a movie folder, or one season folder of a show. A title is moved as a whole: a file that cannot move (still seeding, changed recently) is found before anything is copied, so the folder stays put. If a move is cut short midway (someone starts watching, time runs out), part of a folder can be on each disk until the next run finishes it; that unit gets no cooldown. Loose files at a show's top folder or in a library root stay where they are. Season folders are recognised by name: `Season 1`, `Series 2`, `S03`, and `Specials` (season 0).

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
- A pinned title is never moved to an HDD, by a run or by `move` ([pinned-titles.md](pinned-titles.md)).
- While a conversion session file says a session is open, nothing moves ([contracts.md](contracts.md)).

## Moving one title now

`lombo-tier move NAME [NAME...] [--dry-run]` moves named titles from the SSDs to the HDDs with the checks above:
mounted disks, the destination margin, hash-checked copies, no title that is playing (even with `--no-wait`), no
title still hard-linked to a seeding torrent, no pinned title, the run lock. It is recorded in `state.json` like a
night's run (a last-run note and a cooldown per title) and rewrites `tiers.json`. `NAME` is a title folder name
(`Jaws (1975)`) or its path under the library (`movies/Jaws (1975)`, `tv/Some Show/Season 2`). If a name matches
nothing, or matches several, `move` prints the matches and moves nothing, for any of the names. With `--dry-run`, or
`DRY_RUN=true`, it prints the plan and changes nothing.

## Counting free space

mergerfs stops sending new files to a branch once it has less than `minfreespace` free. So the last `minfreespace` of each SSD is not room for new files. LomboTier counts, for each SSD, only the free space above `RESERVE_GB`, adds one reserve, and counts disks on the same filesystem once. A plain sum over several SSDs would stay above the target forever and nothing would ever move. Set mergerfs `minfreespace` equal to `RESERVE_GB`.

## A run, step by step

1. Check that `state.json` and the pin list (if you use one) are readable, that no conversion session is open
   (if `SESSION_FILE` is set) and, with `CHECK_MOUNTS=true`, that every branch is mounted. If not, stop: nothing
   moves. A live run also takes the run lock first, so only one run or `move` goes at a time.
2. Read what Jellyfin knows about plays, per user, and who is watching now.
3. Scan the SSD branches. The HDD file list is kept in `hdd-index.json` in `DATA_DIR` and refreshed when it is older than `HDD_INDEX_MAX_DAYS`, so sleeping HDDs are not woken every night.
4. Build the plan.
5. If `DRY_RUN=true`, print it and stop. A dry run does not write `state.json` (it does write `tiers.json`, which
   only describes where files are).
6. If there is something to move, walk the HDDs fresh (they are about to wake anyway) and plan again, so the plan matches the disks.
7. Wait up to `WAIT_QUIET_MINUTES` for nobody to be watching. If people keep watching, stop without moving anything.
8. Do the moves, checking Jellyfin (and the session file) every 30 seconds. If someone starts watching, or a
   conversion session opens, the current file stops cleanly and the rest are skipped. With a pin branch, pinned
   titles are gathered onto it after the other moves.
9. Walk the HDDs once more so the saved file list matches, then write `state.json` (last run and a cooldown per
   moved title) and `tiers.json` (where every title is now, see [contracts.md](contracts.md)).
10. Send the webhook summary if something moved or failed, and ping `PING_URL`.
