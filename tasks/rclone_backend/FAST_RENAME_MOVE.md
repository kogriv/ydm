# Fast Rename/Move For Policy-Aware Sync

*Status:* MVP implemented. Created after the 2026-07-14 bidirectional rename
test showed that `rclone bisync` treats a local rename as delete+upload and
waits ~8 minutes for Yandex Disk delete completion.

## Problem

`rclone bisync` is correct but inefficient for renames. In the observed test:

```text
before.txt locally renamed to after.txt
bisync saw:
  - before.txt deleted on Path1
  - after.txt new on Path1
bisync did:
  - copy after.txt to Yandex
  - delete before.txt from Yandex
```

The actual file payload was 56 bytes, but the operation took ~8.5 minutes
because Yandex Disk delete is asynchronous and rclone waits for the remote
operation to finish.

For real renames this is wasteful and noisy:

- uploads content that already exists in the cloud;
- waits for slow delete;
- creates a wider failure window;
- makes scheduled 30-minute jobs easier to overlap.

## Key Fact

The configured rclone `yandex:` backend reports:

```text
Move: true
DirMove: true
Copy: true
```

So a server-side move/rename is available. Yandex Disk also has a
server-side resource move operation. The first implementation should use
`rclone moveto` because it reuses existing auth/config and backend behavior.
Direct Yandex API should remain a fallback/advanced path, not the first
implementation.

## Goals

- Provide an explicit fast rename/move command for policy-safe paths.
- Avoid delete+upload when the user knows the operation is a rename/move.
- Keep all operations dry-run by default.
- Respect `sync_policy.py`: only `bidirectional` paths are eligible for remote
  rename/move in MVP.
- Leave `download_only` paths protected unless a later cloud-normalization plan
  explicitly handles them.
- Keep `bisync` as the background reconciler after explicit moves.

## Non-Goals

- Do not silently infer and apply renames in scheduled jobs in the MVP.
- Do not automatically rename cloud objects for Android-incompatible paths.
- Do not bypass policy with direct API calls unless rclone proves insufficient.
- Do not implement a full conflict resolver in this phase.

## Option A — Explicit `rclone moveto` Fast Path (Recommended MVP)

Add `tools/sync_rename.py`:

```bash
python3 tools/sync_rename.py plan \
  --old /DAO/path/before.txt \
  --new /DAO/path/after.txt

python3 tools/sync_rename.py apply \
  --old /DAO/path/before.txt \
  --new /DAO/path/after.txt
```

Behavior:

1. Load `var/sync_policy.json`.
2. Verify both `old` and `new` are under the same `bidirectional` policy root.
3. Reject `download_only` and `disabled` paths by default.
4. Validate Android/path risk for `new`.
5. Verify local and remote preconditions:
   - local old exists, local new does not exist before local rename;
   - remote old exists;
   - remote new does not exist.
6. Dry-run prints:
   - local `mv`;
   - remote `rclone moveto`;
   - follow-up `sync_bisync.py resync --apply`.
7. Apply order:
   - local `mv old new`;
   - `rclone moveto yandex:old yandex:new`;
   - `sync_bisync.py resync --apply --filter-path .bisync.filters`.

Expected outcome:

- No content upload for same-remote rename.
- No delete+upload cycle.
- `bisync` refreshes its baseline after the explicit move.

Validated:

- `tools/sync_rename.py plan` rejects `/pro/agents` because it is
  `download_only`.
- `tools/sync_rename.py apply` under `/DAO` completed a local rename plus
  Yandex server-side `rclone moveto`.
- The measured `rclone moveto` latency for one small test file was ~5 seconds.
- Follow-up must be `bisync resync --apply`, not ordinary `bisync run`.
  A live validation with `run` produced `..path1`/`..path2` conflict copies
  because both Path1 and Path2 had changed outside bisync's previous baseline.
  The corrected `resync` flow left only `after.txt` locally and remotely, with
  `resync_needed: False`.

## Option B — Direct Yandex Disk API Move

Implement only if Option A is slow, missing behavior, or hard to make robust.

Possible command shape:

```bash
python3 tools/sync_rename.py apply \
  --backend yandex-api \
  --old /DAO/path/before.txt \
  --new /DAO/path/after.txt
```

Pros:

- Direct control over the exact API call.
- Can expose Yandex-specific operation status and polling.
- Can later support richer conflict messages.

Cons:

- Needs token/config plumbing separate from rclone.
- More code and more secret-handling surface.
- Must duplicate retry/polling behavior that rclone already implements.

Decision: keep as fallback. Do not implement before testing `rclone moveto`.

## Option C — Bulk Rename Plans

For batches, add a JSONL/JSON plan format:

```json
{
  "schema": "ydm_rename_plan:v1",
  "items": [
    {
      "old": "/DAO/a.txt",
      "new": "/DAO/b.txt",
      "reason": "manual"
    }
  ]
}
```

Commands:

```bash
python3 tools/sync_rename.py plan-bulk renames.json
python3 tools/sync_rename.py apply-bulk renames.json
```

Rules:

- Validate the whole plan before applying anything.
- Reject duplicate targets and source/target overlap hazards.
- Apply one item at a time with durable progress log under `var/rename.log`.
- Support resume by recording completed items.
- Keep `--max-errors` and stop on first error by default.

This is useful for controlled mass cleanup and for future
`rename_cloud_plan` output.

## Option D — Detect Local Rename After The Fact

Later, add an analyzer for "user already renamed locally":

```bash
python3 tools/sync_rename.py detect \
  --path /DAO
```

Detection idea:

- Compare local scan and cloud/composite scan.
- Find pairs where:
  - one local path is new;
  - one cloud/previous local path is missing;
  - size and md5 match;
  - both are under the same `bidirectional` policy root.
- Present candidates; never apply automatically in MVP.

Apply idea:

```bash
python3 tools/sync_rename.py apply-detected --candidate-id ...
```

Risks:

- False positives when two identical files exist.
- Ambiguous many-to-one matches.
- Stale scans can produce bad candidates.

Decision: implement only after explicit rename works and has tests.

## Option E — Integrate Android Name Normalization

This is related but separate from normal user renames.

Future flow:

```bash
python3 tools/sync_policy.py inspect --path /pro/agents
python3 tools/sync_rename.py plan-normalize --path /pro/agents
```

It would generate a cloud rename plan converting names like:

```text
| -> ｜
: -> ：
```

Then, after explicit review, `/pro/agents` could become `bidirectional`.

Risks:

- Changes original archive names in Yandex Disk.
- May affect links or external references.
- Needs collision checks.

Decision: keep this as a later feature. Do not mix it into ordinary fast
rename MVP.

## Safety Rules

- Dry-run by default.
- `apply` requires explicit command, never hidden inside scheduled jobs.
- Reject paths outside policy.
- Reject `download_only` paths unless a future command explicitly targets
  cloud normalization.
- Reject moves across different policy roots in MVP.
- Reject target already existing locally or remotely.
- Log every apply to `var/rename.log`.
- After apply, run or instruct a `sync_bisync.py resync` against
  `.bisync.filters`.

## Testing Plan

MVP tests:

- `plan` rejects `/pro/agents` because it is `download_only`.
- `plan` accepts a temp file under `DAO`.
- `apply` on temp file under `DAO`:
  - local file is renamed;
  - `rclone moveto` succeeds;
  - cloud listing shows only the new name;
  - follow-up `bisync resync` ends with `last_status=ok`.
- Target-exists checks fail safely.
- Path-outside-policy checks fail safely.

Performance test:

- Compare `rclone moveto` rename latency vs current `bisync` rename latency
  (~8.5 minutes observed).

## Backlog

- [ ] Implement `tools/sync_rename.py plan|apply`.
- [ ] Measure `rclone moveto` latency on Yandex.
- [x] Decide whether follow-up should be `bisync run` or `bisync resync`
      (`resync`; ordinary `run` creates conflict copies after out-of-band
      local+remote move).
- [ ] Add `var/rename.log`.
- [ ] Add bulk plan format and `plan-bulk|apply-bulk`.
- [ ] Add local rename detector.
- [ ] Add direct Yandex API backend fallback if `rclone moveto` is insufficient.
- [ ] Add Android normalization plan for risky `download_only` paths.
