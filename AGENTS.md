# AGENTS.md

Notes for an AI coding agent (or a person) working in this repo. `CLAUDE.md` pulls this file in.

## Contents

- [What this is](#what-this-is)
- [Layout](#layout)
- [Rules](#rules)
- [Test](#test)
- [Skills](#skills-claudeskills)

## What this is

LomboTier is a small Python 3.12 program that runs beside a Jellyfin server and a mergerfs pool.
It moves whole movie folders and TV season folders between the SSD and HDD branches based on what
people watch. It moves real files, so accuracy matters more than speed.
Standard library only (add a dependency only when it clearly earns its place).

## Layout

| Path | What |
|---|---|
| `src/lombo_tier/core.py` | The decisions. Pure functions: no network, disk or clock. Change the rules here. |
| `src/lombo_tier/disks.py` | Scan branches, hash-verified copy, `move_unit`, mount checks. |
| `src/lombo_tier/watch.py` | Jellyfin play data to per-file watch data. |
| `src/lombo_tier/jellyfin.py` | The Jellyfin client. Add calls here. |
| `src/lombo_tier/config.py` | Settings from `LOMBO_TIER_*` environment variables. |
| `src/lombo_tier/state.py` | `state.json`: last run and per-title cooldowns. |
| `src/lombo_tier/mover.py` | Orchestrates a run. |
| `src/lombo_tier/schedule.py` | Seconds until the next run hour. |
| `src/lombo_tier/notify.py` | Webhook alerts. |
| `src/lombo_tier/pins.py`, `session.py`, `tiers.py` | The pin list, the conversion session file (read only) and `tiers.json`. Contracts: `docs/contracts.md`. |
| `src/lombo_tier/cli.py` | `doctor`, `plan`, `run`, `move`, `pin`. |
| `tests/` | pytest, with a fake Jellyfin and webhook in `conftest.py`. |
| `examples/` | systemd unit and timer, mergerfs fstab line. |
| `scripts/leak-check.sh` | Fails when private or personal strings appear. CI runs it. |
| `docs/` | One topic per file, each with a `## Contents` list. |

## Rules

- **Keep `core.py` pure.** If a rule needs data, pass it in. That is what makes it testable.
- **Safe by default.** `DRY_RUN` defaults to true. `plan` must never change anything.
- **Never move files in tests outside temporary folders.** No test may touch a real pool or a real path.
- **A copy is verified by hash before the original is removed.** Do not weaken this. If a pool disk is not mounted, nothing moves.
- **A new setting** goes in `config.py` (validated, message names the variable), `.env.example`
  (one comment line), and `compose.example.yaml` if it needs passing through.
- **Fail with a message, not a traceback.** Wrap outside calls; the loop survives one bad round.
- **Never put private details in the repo:** real hostnames, IPs, webhook URLs, API keys, local
  paths. Use `jellyfin.example.com`, `/mnt/ssd1`, `/mnt/hdd1`, `/media`, `changeme`.
  `scripts/leak-check.sh` enforces it; patterns live in `scripts/leak-patterns.txt`.
- Plain language in docs and messages. Never call anything "official" or imply the Jellyfin project made it.
- The Lombo look (every icon has the Lombo L) is in `docs/branding.md`.

## Test

```sh
pip install -e '.[dev]'
pre-commit install            # once: runs the checks below before every commit
pre-commit run --all-files    # ruff lint + format, strict mypy, file hygiene, leak check
pytest -q --cov               # tests with coverage
```

These are the same checks CI runs (one config: `.pre-commit-config.yaml` and `pyproject.toml`), so what passes
locally passes there. The tooling is the same in every Lombo-Forge repo: ruff for lint and format, strict mypy,
pytest. Do not loosen a rule to make a change pass; fix the code, or say why in a comment next to a narrow `noqa`.

Without Python installed: `docker run --rm -v "$PWD:/w" -w /w python:3.12-slim sh -c "pip install -q -e '.[dev]' && pytest -q"`.
Tests use a fake server. Before claiming a Jellyfin behaviour works, check it against a throwaway
Jellyfin container, never someone's real server.

## Working efficiently

- Optional tools that live in your own profile, not in this repo. Skip any you do not have.
- Graphify: build the code-only graph with `graphify update .` (never `/graphify .`, which costs tokens) and run
  `graphify query "<question>"` before grepping. `graphify-out/` is generated and gitignored; INFERRED edges are guesses.
- Ponytail: the smallest change that fully solves the task. Keep diffs small and focused.

## Skills (`.claude/skills/`)

- `install-into-my-stack`: help a user put the app into their setup, then run `doctor`, `plan` and one `move`.
- `troubleshoot`: read `doctor` output and logs.
- `adapt`: change the rules, thresholds or settings safely.
