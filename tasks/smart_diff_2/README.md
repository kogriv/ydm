# Smart Diff 2: sync-only diff (proposed, not implemented)

**Status: proposed, not built.** This is an open idea, not documentation
of existing behavior — there's no `--sync-only` flag or equivalent
command in the codebase today.

**Idea:** a filtered mode for `report diff` (`--sync-only` or similar)
that only shows paths within the currently *synced* subset — i.e.
respecting `exclude-dirs` (API backend) / the filter-file (rclone
backend), so the diff only surfaces genuinely unexpected discrepancies
rather than "missing" files in folders that were never meant to sync
locally in the first place. Output shape would stay identical to the
current `report diff` — just fewer records and adjusted counts.

**Depends on:**
- `tasks/smart_diff/` — the composite-snapshot diff this would filter.
- `tasks/sync_manager/` — the exclude-dirs/filter-file logic for
  determining what's "in scope" for sync.

If you want to pick this up, see [CONTRIBUTING.md](../../CONTRIBUTING.md).
