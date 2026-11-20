---
name: adapt
description: Change LomboTier's rules, thresholds or settings without breaking it. Use when the user wants different behaviour, a new setting, a new Jellyfin call or a different alert target.
---

# Adapt LomboTier

1. **Change a rule:** the rules are in `core.py` (`mark_hot` for what counts as being watched,
   `plan` for what moves and in what order, `unit_of` for what a unit is). Write the test first with
   plain data, watch it fail, then change the code. Keep `core.py` free of network, disk and clock;
   pass what it needs in (the caller passes `now`).
2. **Add a setting:** read and validate it in `config.py` (the error must name the variable, bad
   settings exit with code 2), add a commented line to `.env.example`, add it to `compose.example.yaml`
   only if it needs passing through, and add a test for a bad value. If it feeds a rule, add the field
   to `core.Settings` and pass it from `config.py`.
3. **Add a Jellyfin call:** add a method to `jellyfin.py`, the path to the fake in `tests/conftest.py`,
   and a test. Confirm it against a throwaway Jellyfin.
4. **Change alerts:** `notify.py` posts to a webhook. For a service that needs a different body, put a
   small bridge in front of it.
5. **Run:** `pytest -q` and `sh scripts/leak-check.sh`. Both must pass.
6. **Stay safe:** `DRY_RUN` must still default to true, `plan` must stay read-only, copies must still
   be hash-verified before the original is removed, and no test may move files outside a temporary folder.
