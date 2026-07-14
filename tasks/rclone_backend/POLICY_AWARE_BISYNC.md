# Policy-Aware Bidirectional Sync — Gap Doc And Design

*Status:* MVP implemented locally on 2026-07-14. Created after the
`/pro/agents` Android filename incident.

## Context

`ydm` currently has two rclone-based sync mechanisms:

- `tools/sync_filters.py add/remove` edits one filter-file and materializes
  selected cloud paths locally with `rclone copy`.
- `tools/sync_bisync.py run/resync/status` uses the same filter-file with
  `rclone bisync` for bidirectional sync, triggered every 30 minutes from
  Termux via `ydm_bisync_job.sh`.

This worked while every included path was representable 1:1 on Android shared
storage. It broke as a design assumption when `/pro/agents` was added: Yandex
Disk accepted filenames containing ASCII `|` and `:`, while `/sdcard` rejected
creating those paths with `Operation not permitted`.

The immediate repair copied the incompatible files with local-safe lookalike
characters (`|` -> `｜`, `:` -> `：`). That restored the local mirror, but it
made the tree unsafe for blind `bisync`: a later bidirectional run could treat
the local safe names as canonical, delete the original cloud names, and upload
the renamed local variants.

## Gap

The project lacks an explicit policy layer between "I want this path locally"
and "this path is safe to modify bidirectionally".

Current behavior conflates these concerns:

```text
/path in /sdcard/Download/ya_disk.filters
  means both:
  - download/materialize this path locally
  - include this path in scheduled bidirectional sync
```

That is too coarse for Android/proot environments because a path can be useful
as a local mirror but unsafe as a bidirectional root.

## Design Goals

- Bidirectional sync remains the default for safe paths.
- Risky paths are detected before they enter the bidirectional set.
- The user gets an explicit decision, not a hidden automatic rename.
- Download-only mirrors are first-class, not a failed/partial state.
- Scheduled jobs must never use an unsafe path just because it exists in a
  materialization filter.
- All dangerous operations remain dry-run first and machine-readable.

## Non-Goals

- Do not build a general filesystem abstraction over every cloud/local backend.
- Do not silently normalize cloud filenames.
- Do not make `rclone bisync` responsible for policy decisions; it should only
  receive a generated safe filter.
- Do not solve full conflict resolution in this phase. The first version is a
  safety gate plus explicit path modes.

## Proposed Model

Add a policy file owned by `ydm`, for example:

Recommended source of truth for this environment:

```text
/root/notes/pro/ydm/var/sync_policy.json
```

Rationale: the policy is `ydm` operational state, not a cloud object and not a
user-facing file in the Android Downloads tree. Keeping it under `var/` makes
it available inside proot-Debian, easy to inspect/version during development,
and separate from generated rclone filters. Generated filters should still live
next to the local mirror because rclone invocations already expect that
convention:

```text
/sdcard/Download/ya_disk.download.filters
/sdcard/Download/ya_disk.bisync.filters
```

```json
{
  "schema": "ydm_sync_policy:v1",
  "local_root": "/sdcard/Download/ya_disk",
  "remote": "yandex",
  "paths": {
    "DAO": {
      "mode": "bidirectional"
    },
    "pro/mathcoach": {
      "mode": "bidirectional"
    },
    "video/Obsidian": {
      "mode": "bidirectional"
    },
    "pro/agents": {
      "mode": "download_only",
      "reason": "android_incompatible_names",
      "risk_scan_id": 1
    }
  }
}
```

Supported modes:

- `bidirectional`: path is included in the scheduled `rclone bisync` filter.
- `download_only`: path is materialized locally with `rclone copy`/repair tools
  but excluded from scheduled `bisync`.
- `disabled`: remembered by policy but not currently materialized or bisynced.

Generate separate filter files from policy:

```text
/sdcard/Download/ya_disk.download.filters
  includes bidirectional + download_only paths

/sdcard/Download/ya_disk.bisync.filters
  includes only bidirectional paths + RCLONE_TEST
```

The existing `/sdcard/Download/ya_disk.filters` can be retained as a legacy
compatibility path during migration, but new scheduled jobs should use the
generated bisync filter explicitly.

## Risk Analyzer

Before adding a path to `bidirectional`, inspect the cloud snapshot for risks.

Initial risk checks:

- Android forbidden path characters for `/sdcard`:
  - `<`, `>`, `:`, `"`, `|`, `?`, `*`, `\`
- Sanitization collisions:
  - two cloud paths map to the same local-safe path
- Case-folding collisions if the local filesystem behaves case-insensitively
  for a mounted path
- Unicode normalization collisions
- Path length near Android/FUSE limits
- Existing local names that differ from cloud names only by sanitization
- Pending local changes under a path before first `resync`

The analyzer should produce a structured report:

```json
{
  "schema": "ydm_sync_risk:v1",
  "path": "/pro/agents",
  "safe_for_bidirectional": false,
  "risks": [
    {
      "code": "android_incompatible_names",
      "severity": "blocker",
      "count": 13,
      "examples": [
        "pro/agents/agents_week/Agents Week 2026 | ..."
      ]
    }
  ],
  "choices": [
    "download_only",
    "rename_cloud_plan",
    "abort"
  ]
}
```

## User Decisions

For a risky path, the tool should offer explicit choices:

- `download_only`: keep the local mirror, exclude from scheduled bidirectional
  sync. This is the safest default.
- `rename_cloud_plan`: generate a dry-run plan that renames cloud objects to
  local-safe names, then allows the path to become bidirectional after review.
- `abort`: do not change policy or filters.

Potential later choices:

- `ignore_risk`: force bidirectional with an explicit allowlist marker. This
  should require a long flag like `--force-risk android_incompatible_names`,
  not be the default CLI path.

## Command Shape

Possible CLI surface:

```bash
# Inspect without changing anything.
python3 tools/sync_policy.py inspect --path /pro/agents

# Add with default policy decision:
# safe path -> bidirectional
# risky path -> blocked with choices
python3 tools/sync_policy.py add --path /pro/agents

# Explicitly add as download-only.
python3 tools/sync_policy.py add --path /pro/agents --mode download_only --apply

# Generate filters after policy changes.
python3 tools/sync_policy.py render-filters --apply

# Show current effective sets and resync status.
python3 tools/sync_policy.py status
```

`sync_filters.py` can either become a thin wrapper around this policy layer or
remain as a lower-level legacy command with warnings.

## Recommended MVP

Implement the first version in phases. Do not start with cloud rename logic.
The immediate operational goal is to restore safe scheduled bidirectional sync
while keeping risky trees available as download-only mirrors.

Phase 1:

1. [x] Add `tools/sync_policy.py status|inspect|render-filters`.
2. [x] Add risk analysis from the existing cloud snapshot.
3. [x] Add migration from the current legacy filter to `var/sync_policy.json`.
4. [x] Generate `.download.filters` and `.bisync.filters`.
5. [x] Mark `/pro/agents` as `download_only` and keep the old safe paths
   `bidirectional`.

Phase 2:

1. [x] Update `ydm_bisync_job.sh` to use only `.bisync.filters`.
2. [x] Run `sync_bisync.py resync --apply --filter-path ...bisync.filters`.
3. [x] Verify manual `run --apply` succeeds with `run OK`.
4. [x] Keep `sync_filters.py` as a legacy/low-level command, but make it warn
   when changing filters without policy.

Phase 3:

1. [x] Add policy-aware `add/remove` commands.
2. [x] Make safe paths default to `bidirectional`.
3. [x] Make risky paths default to blocked unless `download_only` or
   `--force-risk` is explicit.
4. [ ] Add tests/fixtures.

Phase 4:

1. Implement `rename_cloud_plan` as a dry-run report.
2. Only after review, add an explicit apply command for cloud renames.

## Rename Cloud Policy

`rename_cloud_plan` is intentionally not part of the MVP apply path.
The broader fast rename/move design lives in
[`FAST_RENAME_MOVE.md`](FAST_RENAME_MOVE.md).

For the first version it may be a report/stub:

```text
rename_cloud_plan: not implemented
reason: first release only separates safe bidirectional paths from download-only paths
```

When implemented, it must stay dry-run by default and should never run as part
of `sync_policy add` automatically. Renaming cloud objects is a semantic change
to the user's archive, not just a local filesystem workaround.

## Scheduled Job Change

The Termux scheduled job should use only the generated bidirectional filter:

```bash
python3 tools/sync_bisync.py run --apply \
  --local-root /sdcard/Download/ya_disk \
  --filter-path /sdcard/Download/ya_disk.bisync.filters
```

This prevents a download-only path from entering `bisync` accidentally.

## Migration Plan

1. Read the existing `/sdcard/Download/ya_disk.filters`.
2. Seed policy with all existing include paths as `bidirectional`.
3. Run risk analysis for each path.
4. Downgrade blocker-risk paths to `download_only` unless the user explicitly
   chooses a rename-cloud plan.
5. Generate `.download.filters` and `.bisync.filters`.
6. Leave legacy `/sdcard/Download/ya_disk.filters` in place during the first
   migration. Treat it as compatibility/download state, not as the scheduled
   bisync source.
7. Update `ydm_bisync_job.sh` to use `.bisync.filters`.
8. Run `sync_bisync.py status` and require `resync --apply` only for the safe
   bidirectional filter.

Current local recommendation after the `/pro/agents` incident:

```text
DAO             -> bidirectional
pro/mathcoach   -> bidirectional
video/Obsidian  -> bidirectional
pro/agents      -> download_only
```

Safe rollout order for the live device:

1. Generate policy and filters in dry-run.
2. Inspect that `.bisync.filters` contains only:
   - `DAO`
   - `pro/mathcoach`
   - `video/Obsidian`
   - `RCLONE_TEST`
3. Inspect that `.download.filters` additionally contains `pro/agents`.
4. Update the Termux scheduled job to use `.bisync.filters`.
5. Run one manual `resync --apply` against `.bisync.filters`.
6. Run one manual `run --apply` against `.bisync.filters`.
7. Confirm `ydm-bisync-status` shows no `resync_needed`.
8. Let the next 30-minute scheduled job run and confirm `run OK`.

Local rollout result on 2026-07-14:

- `sync_policy.py inspect --path /pro/agents` found 13 blocker paths and
  recommended `download_only`.
- `sync_policy.py migrate --apply` wrote `var/sync_policy.json`.
- `sync_policy.py render-filters --apply` wrote both generated filters.
- `ydm_bisync_job.sh` now uses `/sdcard/Download/ya_disk.bisync.filters`.
- Manual `resync --apply` on `.bisync.filters` succeeded with no transfers.
- Manual `run --apply` on `.bisync.filters` succeeded with `No changes found`.

## Acceptance Criteria

- Adding a path with Android-incompatible names no longer blocks the entire
  scheduled bidirectional sync.
- The scheduled job never reads the download-only filter.
- `ydm-bisync-status` makes it clear which paths are bidirectional and which
  are download-only.
- A risky path cannot become bidirectional without an explicit user decision.
- A dry-run rename plan is available before any cloud rename is performed.
- The `/pro/agents` case reports 13 incompatible paths and recommends
  `download_only` by default.
