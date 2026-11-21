# Contracts with other tools

## Contents

- [tiers.json: where each title lives](#tiersjson-where-each-title-lives)
- [The conversion session file](#the-conversion-session-file)
- [The pin list](#the-pin-list)
- [The post-move command](#the-post-move-command)
- [What changing a version means](#what-changing-a-version-means)

LomboTier shares small JSON files with other tools. Each has a `version`. Read the version first and ignore a
file whose version you do not know.

## tiers.json: where each title lives

Written to `tiers.json` in `LOMBO_TIER_DATA_DIR` after every run and every live `move`. A web-client plugin that
shows storage tags, or a dashboard, may read it. It is replaced atomically (a temporary file renamed over the old
one), so a reader sees the old file or the new one, never half of one. It is stable: this shape only gains fields
inside a version.

```json
{
  "version": 1,
  "updated": 1767225600,
  "units": [
    {"path": "movies/Jaws (1975)", "tier": "ssd", "bytes": 4294967296, "pinned": true},
    {"path": "tv/Some Show/Season 2", "tier": "hdd", "bytes": 21474836480, "pinned": false}
  ]
}
```

| Field | Meaning |
|---|---|
| `version` | Always `1` for this shape. |
| `updated` | When the file was written, in unix seconds. |
| `units` | One row per movie folder or show-season folder, sorted by `path`. |
| `path` | The folder, relative to the pool root, using `/`. This is the same path inside Jellyfin after its prefix. |
| `tier` | `"ssd"` or `"hdd"`. A title cut short midway (files on both) is `"ssd"` until the next run finishes it. |
| `bytes` | Size of the files in the folder. |
| `pinned` | `true` when the pin list covers this folder. |

Notes for readers:

- A show's season folders are separate rows. Loose files at a show's top folder are not listed.
- The file can be a day old. Dry runs write it too, because it describes where files are, not what would move. A run
  that stands down (damaged state file or pin list, an open conversion session, a disk that is not mounted) does not
  write it, so the last good copy stays.
- `updated` is the time to check if you need to know how fresh it is.

## The conversion session file

This is a **proposal** that LomboShrink (or any converter) can implement. LomboTier already reads it; LomboShrink
does not write it yet.

While a tool is converting media it may keep a small file. LomboTier does not move anything while that file exists
and has not expired. Set `LOMBO_TIER_SESSION_FILE` to its path. Both tools must see the same file, so put it in a
folder both containers mount. With no setting, LomboTier does not check.

```json
{"version": 1, "owner": "lombo-shrink", "expires": 1767229200}
```

| Field | Meaning |
|---|---|
| `version` | `1`. |
| `owner` | The tool's name. Not empty. Shown in LomboTier's messages. |
| `expires` | Unix seconds. After this moment the session counts as closed, so a crashed converter cannot block moves forever. |

The rules LomboTier follows:

| The file is | LomboTier |
|---|---|
| missing | goes ahead |
| valid and `expires` is in the future | **open**: moves nothing, says who holds it. This is not an error and sends no alert |
| valid and `expires` has passed | goes ahead (closed) |
| there but unreadable: not JSON, the wrong version, an empty owner, no `expires`, an empty file | **stands down** and says why. This is a failure, `doctor` shows FAIL. An unreadable lock is never read as closed |

What the writer should do:

- Write the file atomically (write a temporary file, then rename it), so a reader never sees half of it.
- Set `expires` a little ahead and refresh it while the session lasts. Delete the file when the session closes.
- If a session opens while LomboTier is moving files, LomboTier notices within about 30 seconds and stops at the
  next safe point. A title mid-copy finishes or stops cleanly (the copy is hash-checked; the original is only removed
  after a verified copy). To be sure, wait for LomboTier's current run to end before touching the same folders.

LomboTier only ever reads this file. It never creates, changes or deletes it.

## The pin list

Written by `lombo-tier pin add` and `pin remove`. Other tools may read it; a tool that writes it should do so
atomically and keep the shape. Details are in [pinned-titles.md](pinned-titles.md).

```json
{"version": 1, "titles": ["movies/Jaws (1975)", "tv/Some Show"]}
```

## The post-move command

- **Name:** `LOMBO_TIER_POST_MOVE_COMMAND`. Empty (the default) means off.
- **When it runs:** once per live run or `move` in which at least one unit was demoted, promoted or gathered, after
  `tiers.json` is written. Never in a dry run, never when nothing moved. Not once per title.
- **How:** split with `shlex`, started without a shell, no input, output dropped, 60 second limit.
- **What it gets:** the normal environment plus `LOMBO_TIER_MOVED_JSON`, a JSON object with the pool-relative unit
  paths:

```json
{"demoted": ["movies/Jaws (1975)"], "promoted": [], "gathered": ["tv/Some Show/Season 1"]}
```

- **Failure never fails the action:** a non-zero exit, a timeout or a command that cannot start adds a note to the
  result's `note` and to the last-run entry in `state.json` (for example `ok; post-move command failed (exit 1)`)
  and a log line. The moves, `failed`, the webhook and the exit code are unchanged.

## What changing a version means

- Adding a field inside a version is not a change: readers must ignore fields they do not know.
- Renaming or removing a field, or changing what one means, raises `version`. LomboTier then writes the new
  version only, and this page lists the old and the new shape. A reader that sees a version it does not know should
  do nothing rather than guess.
- LomboTier refuses to read a session file or a pin list with a version it does not know, and says so.
