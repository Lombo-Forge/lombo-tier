# How it is built

## Contents

- [The shape](#the-shape)
- [Modules](#modules)
- [Why it is split this way](#why-it-is-split-this-way)
- [Safe by default](#safe-by-default)
- [Settings](#settings)
- [Alerts](#alerts)
- [Adding a Jellyfin call](#adding-a-jellyfin-call)

## The shape

```
Jellyfin --> jellyfin.py --> watch.py --+
                                        +--> mover.py --> core.py (decides)
disks.py (scan, copy, checks) ----------+        |
state.py (state.json) -------------------+       +--> notify.py --> webhook
cli.py calls mover.py, schedule.py
```

`mover.py` gathers data, hands it to `core.py`, then acts on the answer through `disks.py`.
`core.py` never touches the network, the disk or the clock.

## Modules

| Module | Job |
|---|---|
| `core.py` | Pure planner. Units, hot and cold rules, the plan. |
| `disks.py` | Scan branches, hash-verified copy, `move_unit`, mount checks. |
| `watch.py` | Turns Jellyfin play data into per-file watch data. |
| `jellyfin.py` | The Jellyfin client. |
| `config.py` | Settings from environment variables. |
| `state.py` | `state.json`: last run and per-title cooldowns. |
| `mover.py` | Orchestrates a run. |
| `schedule.py` | Seconds until the next run hour. |
| `notify.py` | Webhook. |
| `pins.py` | The pin list: read, validate, write atomically, match a unit. |
| `session.py` | Reads the conversion session file (never writes it). |
| `tiers.py` | Builds and writes `tiers.json`. |
| `cli.py` | `doctor`, `plan`, `run`, `move`, `pin`. |

## Why it is split this way

Rules are the part that changes most and breaks most quietly. `plan()` takes plain data and
returns plain data, so a test needs no server and no disk. The I/O layers are thin. Tests that
move files do it only in temporary folders.

## Safe by default

- `DRY_RUN` is true until the person sets it to false.
- `plan` never changes anything.
- A copy is verified by hash before the original is removed.
- If a pool disk is not mounted, nothing moves.
- A bad setting stops the app at start-up and names the variable (exit code 2).
- In `--loop` mode one failed round is printed and the next one still runs.

## Settings

Every setting is an environment variable with the `LOMBO_TIER_` prefix, validated in `config.py`.
To add one: read it in `config.py` with a clear error, add a line to `.env.example`, add a test.

## Alerts

`notify.py` posts a summary to `WEBHOOK_URL` when a run moved something or failed. `PING_URL` gets a
GET on success and a GET on `<url>/fail` on failure, and is never logged. Both are optional.

## Adding a Jellyfin call

Add a method to the client in `jellyfin.py`, add the path to the fake in `tests/conftest.py`, and
test it. The API reference for your server is at `https://your-server/api-docs/swagger`. Try a new
call against a throwaway Jellyfin first.
