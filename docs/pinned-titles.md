# Pinned titles

## Contents

- [What pinning does](#what-pinning-does)
- [Setting it up](#setting-it-up)
- [Commands](#commands)
- [A dedicated disk for pinned titles](#a-dedicated-disk-for-pinned-titles)
- [If the list is damaged](#if-the-list-is-damaged)
- [Limits](#limits)

## What pinning does

A pinned title is never moved off the SSD. The nightly run leaves it out of the move-to-HDD plan, and
`lombo-tier move` skips it and says so. Use it for titles that must always be quick to start, or that you do not
want on a spun-down disk.

Pinning a show covers all of its seasons. Pinning one season covers just that season.

## Setting it up

Set `LOMBO_TIER_PIN_FILE` to a path in a folder that survives restarts, for example next to `state.json` in the
data folder (`/data/pins.json`). With no setting, nothing is pinned and the `pin` commands say so.

The file looks like this:

```json
{"version": 1, "titles": ["movies/Jaws (1975)", "tv/Some Show"]}
```

An entry is a title folder name (`Jaws (1975)`) or a path inside the library (`movies/Jaws (1975)`). `pin add`
writes the full library path. You may also edit the file by hand. A missing file means nothing is pinned.

## Commands

| Command | What it does |
|---|---|
| `lombo-tier pin add NAME [NAME...]` | Adds titles. `NAME` must exist on one of your disks: a movie folder, a show folder or one season folder. A name that matches nothing, or matches several (say a movie and a show with the same name), is an error that lists the matches; use the library path to choose. |
| `lombo-tier pin remove NAME [NAME...]` | Removes titles. An error lists what is pinned if the name is not there. |
| `lombo-tier pin list` | Shows the pinned titles. |

These only change the pin list, never a media file, so they work whatever `DRY_RUN` says. The list is written
atomically. `plan`, `doctor` and `tiers.json` show what is pinned.

## A dedicated disk for pinned titles

Set `LOMBO_TIER_PIN_BRANCH` to one of your `LOMBO_TIER_SSD_BRANCHES` (it needs `LOMBO_TIER_PIN_FILE` too). The
nightly run then also **gathers** pinned titles onto that disk, from any other disk, and leaves them there. Gathering
uses the same hash-checked copy, the same space margin and the same checks as any other move. It counts against
`MAX_GB_PER_RUN` separately, and it never touches a title that is playing or still seeding. `plan` lists the titles
it would gather.

If the pin branch is missing or not mounted, nothing about pinned titles moves: they are not gathered, and they are
still never moved to an HDD. With `CHECK_MOUNTS=true` an unmounted pin branch stops the whole run, like any other
unmounted disk.

Without `PIN_BRANCH`, pinning only protects a title from leaving the SSD. A pinned title that is on an HDD today
stays there until the normal rules bring it back (it is being watched), or until you set a pin branch.

## If the list is damaged

If the pin file exists but cannot be read (not JSON, an unknown `version`, `titles` not a list of names, an entry
that is empty or contains `..`), LomboTier does **not** read it as an empty list. That could move every pinned title.
Instead:

- the nightly run and `move` stand down: no moves at all, no change to `state.json`, and the reason is in the note;
- `plan` shows the reason and plans nothing;
- `doctor` shows `[FAIL] pin list`;
- `pin add` and `pin remove` refuse to write, so a damaged file is never overwritten.

Fix the file (or move it aside to start a new list) and the next run carries on.

## Limits

- Pinning is by folder. A title that is renamed on disk needs to be pinned again under its new name.
- Pinning does not protect a title from being deleted, only from being moved by LomboTier.
- The word "pinned" is also used for a different thing: `PIN_DAYS` is the number of days a title that just came back
  to the SSD is left alone. That setting is unrelated to this list.
