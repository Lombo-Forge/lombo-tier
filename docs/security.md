# Security notes

## Contents

- [What LomboTier trusts](#what-lombotier-trusts)
- [What it refuses](#what-it-refuses)
- [What it never logs](#what-it-never-logs)
- [Files it reads and writes](#files-it-reads-and-writes)

LomboTier listens on no port. It makes outgoing calls to Jellyfin, to an optional webhook and to an optional ping
URL. It moves real files, so most of the care is about files and about not acting on bad input.

## What LomboTier trusts

- **The settings** (environment variables). A bad value stops it at start-up with a message.
- **The pin list and the conversion session file** are read as untrusted input. They are parsed as JSON only,
  never run, and every field is type-checked. Anyone who can write them can pin titles or hold moves back; so
  keep both in folders only the apps that need them can write to.
- **Jellyfin's answers** about what is playing. If Jellyfin cannot be asked, LomboTier assumes someone is watching.
- **The disks.** A branch counts as mounted only if its marker folder exists (or it is a mount point).

## What it refuses

- A title name with `..` in it, or one that is empty, in `move`, `pin add`, `pin remove` and in the pin list. Names
  are only ever looked up under the configured branches and library folders, so a name cannot reach another place.
- A pin list, session file or state file it cannot read: it stands down and moves nothing, rather than guess.
- A session file with an unknown version, an empty owner or a missing expiry. It is treated as "someone holds it",
  never as closed.
- Moving a title that is playing (even with `--no-wait`), pinned, hard-linked to a seeding torrent, changed within
  the last hour, or when the destination would have less than `DEST_MARGIN_GB` free.
- Two moves at once. A run and a `move` (or two `move`s) take a lock on `run.lock` in the data folder; the second
  one does not start. The lock goes away with the process, so a crash never leaves it stuck. A dry run does not
  need it.
- Removing an original before its copy has been read back and matched by hash.

## What it never logs

- The Jellyfin API key, the webhook URL and the ping URL (they can hold secrets). A failed ping logs only the kind of
  error.
- The contents of the pin list or the session file. Messages name the file path and the reason (for example "not a
  JSON object"), and the session owner's name, which is a tool name and not a secret.
- Passwords or tokens of any kind: LomboTier handles none besides the Jellyfin key, which is sent only to Jellyfin.

## The post-move command

`LOMBO_TIER_POST_MOVE_COMMAND` is the owner's own setting, like the rest of the environment, so LomboTier runs it as
given. It is split like a shell would split it, but started without a shell: no pipes, no `$VAR`, no globbing. It runs
as the same user as LomboTier, with the same environment plus `LOMBO_TIER_MOVED_JSON` (the pool-relative paths of the
units that moved, which are library paths and nothing secret). It gets no input, its output is thrown away, and it is
stopped after 60 seconds. A failure never fails a run. LomboTier never logs the command's environment. An unbalanced
quote is a start-up error that names the variable. Do not point it at anything that builds a shell command from the
paths.

## Files it reads and writes

| File | Read | Written |
|---|---|---|
| `state.json`, `hdd-index.json`, `tiers.json` in the data folder | yes | yes, atomically (temporary file, then rename) |
| `run.lock` in the data folder | opened to take the lock | created empty, never removed |
| the pin list (`LOMBO_TIER_PIN_FILE`) | yes | only by `pin add` and `pin remove`, atomically, and never when the file is damaged |
| the session file (`LOMBO_TIER_SESSION_FILE`) | yes | never |
| media files on the branches | yes | moved only as described in [how-it-works.md](how-it-works.md) |

`tiers.json` holds library paths and sizes, nothing else. Anyone who can read the data folder can read it, so
mount it read-only into tools that only display it.
