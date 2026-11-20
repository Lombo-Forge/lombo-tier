---
name: install-into-my-stack
description: Put LomboTier into someone's media server setup. Use when the user wants to install it, connect it to their Jellyfin and mergerfs pool, or fill in .env and compose.
---

# Install LomboTier into my stack

This tool moves real files. Go slowly and keep DRY_RUN true until the last step.

1. **Ask about the pool.** Is it mergerfs? Which folders are the individual disks (the branches),
   which are SSDs and which are HDDs? What is the pool mount, and what are the library folders
   inside it (for example `movies` and `tv`)? If the SSDs are not first in the branch order, or
   `category.create` and `minfreespace` are not set, show `examples/mergerfs-fstab.example` and
   explain that `minfreespace` should match `LOMBO_TIER_RESERVE_GB`. Do not edit their fstab for them.
2. **Ask how Jellyfin sees the pool.** The path where Jellyfin sees the pool root is
   `LOMBO_TIER_JELLYFIN_PATH_PREFIX` (for example `/media`). Ask for one real file path from
   Jellyfin's media info if they are unsure.
3. **Jellyfin key.** Dashboard > API Keys > + for a key named for this app.
4. **Ownership.** Find the user that owns the media files (`stat -c '%u:%g' <folder>`) and use it as
   `user: "UID:GID"` in compose. The container needs read-write access to each branch folder, mounted
   at the same path as in `LOMBO_TIER_SSD_BRANCHES` and `LOMBO_TIER_HDD_BRANCHES`.
5. **Write `.env` and compose.** Start from `.env.example` and `compose.example.yaml`. Leave
   `LOMBO_TIER_DRY_RUN=true`. Put no real values in files that go in the repo.
6. **Start and check:** `lombo-tier doctor`. Fix every failure first (see the `troubleshoot` skill).
7. **Look at the plan:** `lombo-tier plan`. Go through it with the user. Do the reasons make sense?
8. **Try one move, still dry:** `lombo-tier move "<a small title>"` with DRY_RUN true.
9. **Only then go live.** With the user's agreement, set `LOMBO_TIER_DRY_RUN=false`, restart, and move
   one small title. Check the files are on an HDD branch and Jellyfin still plays it.
10. Optionally set up the nightly systemd timer from `examples/` instead of `--loop`.

Never run a real move on the user's pool without their explicit yes.
