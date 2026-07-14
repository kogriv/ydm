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
- Make the ordinary user flow safe later: user renames in a file manager,
  `ydm` detects it before scheduled `bisync` turns it into delete+upload.
- Keep all operations dry-run by default.
- Respect `sync_policy.py`: only `bidirectional` paths are eligible for remote
  rename/move in MVP.
- Leave `download_only` paths protected unless a later cloud-normalization plan
  explicitly handles them.
- Keep `bisync` as the background reconciler after explicit moves.

## Non-Goals

- Do not silently infer and apply renames in scheduled jobs in the explicit
  command MVP.
- Do not automatically rename cloud objects for Android-incompatible paths.
- Do not bypass policy with direct API calls unless rclone proves insufficient.
- Do not implement a full conflict resolver in this phase.

## User Flow Target

Current state:

```text
User renames /sdcard/Download/ya_disk/DAO/a.txt -> b.txt in Total Commander
scheduled sync_bisync.py run sees: a.txt deleted + b.txt new
result: correct final state, but delete+upload and slow Yandex delete wait
```

Target state:

```text
User renames /sdcard/Download/ya_disk/DAO/a.txt -> b.txt in Total Commander
ydm preflight sees a probable rename before scheduled bisync
ydm applies server-side rclone moveto yandex:DAO/a.txt yandex:DAO/b.txt
ydm runs bisync resync to refresh baseline
scheduled bisync later sees no pending change
```

The user should not need to know about `sync_rename.py` for ordinary file
manager work. The explicit command remains useful for scripts, advanced users,
and recovery.

## Product UX Layers

Layer 1, implemented: explicit safe command.

```bash
python3 tools/sync_rename.py plan --old /DAO/a.txt --new /DAO/b.txt
python3 tools/sync_rename.py apply --old /DAO/a.txt --new /DAO/b.txt
```

Layer 2, next: human-friendly wrappers.

```bash
ydm-rename /DAO/a.txt /DAO/b.txt
ydm-rename-status
ydm-rename-detect --path /DAO
ydm-rename-apply --candidate-id ...
```

Layer 3, next: AI/CLI-friendly machine interface.

```bash
python3 tools/sync_rename.py detect --format jsonl
python3 tools/sync_rename.py apply-detected --candidate-id ... --format json
```

Layer 4, later: automated pre-bisync guard in the scheduled Termux job.

```text
ydm_bisync_job.sh
  -> detect local renames
  -> auto-apply only high-confidence safe candidates
  -> resync if any fast rename was applied
  -> otherwise normal bisync run
```

Implemented v1 note: scheduled job currently runs detector in `observe` mode
before normal `bisync run`; detector failure is fail-open and does not block
scheduled sync.

Layer 5, optional: near-real-time watcher. This may use Termux-side tools
if Android shared storage emits usable events. Treat watcher as an optimization,
not the correctness foundation; polling/snapshot detection must still work.

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

### Detection Design

Rename detection should run before scheduled `bisync run`, not after it. Once
ordinary bisync has uploaded the new path and deleted the old path, the fast
server-side rename opportunity is gone.

Inputs:

- Previous successful local scan from `monitor.db`.
- Current local scan of the bidirectional policy roots.
- Current cloud/composite scan, or remote existence checks for candidates.
- `var/sync_policy.json` to restrict detection to `bidirectional` roots.
- `var/bisync_state.json` to require a clean baseline before auto-apply.

Candidate shape:

```json
{
  "id": "stable-hash",
  "old": "/DAO/a.txt",
  "new": "/DAO/b.txt",
  "policy_root": "DAO",
  "confidence": "high",
  "reason": ["same_size", "same_md5", "old_missing_local", "new_missing_cloud"],
  "requires_review": false
}
```

High-confidence file candidate:

- `old` existed in the previous local successful scan.
- `old` is missing in the current local scan.
- `new` did not exist in the previous local successful scan.
- `new` exists in the current local scan.
- Both paths are under the same `bidirectional` policy root.
- Size matches.
- MD5 matches, or a local hash computed on demand matches a cloud/DB hash.
- Remote old exists and remote new does not exist.
- No duplicate candidate with the same hash/size exists in the same root.

Directory candidate:

- Prefer explicit `ydm-rename` for directories in the first automated release.
- Automated directory detection is allowed later only if the whole subtree
  fingerprint matches: file count, total size, and a stable sample/full hash
  set all match.
- If any file in the subtree changed in content, require manual review.

Auto-apply rule:

- Only high-confidence file candidates are auto-applied by the scheduled job.
- Ambiguous candidates are logged and shown in `ydm-rename-status`; ordinary
  `bisync run` may either proceed normally or be blocked by policy depending
  on the configured safety mode.

Safety modes:

```text
rename_preflight=observe   # log candidates, never block bisync
rename_preflight=guard     # block bisync when ambiguous rename-like changes exist
rename_preflight=auto      # auto-apply high-confidence, guard ambiguous
```

Recommended rollout:

1. Start with `observe`.
2. Move to `guard` after logs look sane.
3. Enable `auto` only for selected policy roots after live validation.

### Scheduled Job Integration

Current scheduled job:

```text
sync_bisync.py run --apply --filter-path /sdcard/Download/ya_disk.bisync.filters
```

Target scheduled job:

```text
sync_rename.py detect --format json --local-root /sdcard/Download/ya_disk
if safe auto candidates:
  sync_rename.py apply-detected --auto-safe --followup resync
elif ambiguous candidates and mode=guard/auto:
  stop, notify, log; do not run bisync
else:
  sync_bisync.py run --apply
```

Important: if any fast rename is applied, follow-up must be `resync`, not
ordinary `run`.

### State And Logs

Add:

```text
var/rename_candidates.jsonl
var/rename_preflight_state.json
var/rename_apply_last.json
```

Log enough for AI agents and humans:

- candidate id;
- old/new paths;
- policy root and mode;
- confidence;
- exact checks that passed/failed;
- whether scheduled `bisync` was allowed, blocked, or replaced by `resync`.

### Human Output

Narrow-screen first:

```text
rename preflight: 2 candidates, 1 auto-safe, 1 review

AUTO  DAO/a.txt -> DAO/b.txt          md5 match, remote old exists
HOLD  DAO/x.txt -> DAO/y.txt          duplicate same-size/hash candidates

bisync: blocked until review
next: ydm-rename-status --show HOLD
```

JSONL first for agents:

```json
{"kind":"candidate","id":"...","old":"/DAO/a.txt","new":"/DAO/b.txt","confidence":"high"}
{"kind":"decision","bisync":"blocked","reason":"ambiguous_candidates"}
```

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

## Guard/Auto Implementation Plan

Status: planned. `observe` v1 is implemented and enabled in the scheduled
Termux job. The next implementation should add the full safety stack now, but
keep `auto` opt-in rather than the silent default.

### Mode Semantics

`observe`:

- Always allow the normal scheduled `bisync run`.
- Write candidates and decision logs.
- Never notify unless detector itself crashes repeatedly.

`guard`:

- Allow normal `bisync run` when there are no candidates.
- Allow normal `bisync run` when only high-confidence candidates exist and
  `auto` is not enabled. This preserves data correctness while still surfacing
  the faster manual path.
- Block normal `bisync run` when any `review` or `blocked` candidate exists.
- Send a Termux notification with the short reason and point to
  `ydm-rename-status`.

`auto`:

- Auto-apply all high-confidence file candidates.
- Run one `sync_bisync.py resync --apply` after all successful server-side
  moves, not after each candidate.
- Block normal `bisync run` if any review/blocked candidate remains.
- If all high-confidence candidates are applied and no ambiguous candidates
  remain, skip normal `bisync run` because the final `resync` already refreshed
  the baseline.

Default rollout:

```text
repo default: observe
recommended user default after implementation: guard
auto: explicit opt-in only
```

### Configuration

Add `var/rename_policy.json`:

```json
{
  "schema": "ydm_rename_policy:v1",
  "default_mode": "guard",
  "max_auto_candidates": 10,
  "roots": {
    "DAO": {"mode": "guard"},
    "pro/mathcoach": {"mode": "guard"},
    "video/Obsidian": {"mode": "guard"}
  }
}
```

Rules:

- If the file is absent, use built-in default `observe`.
- Root setting overrides `default_mode`.
- `download_only` and `disabled` policy roots are never eligible for auto.
- `auto` is accepted only for file candidates with `confidence=high`.

Commands:

```bash
python3 tools/sync_rename.py policy-status
python3 tools/sync_rename.py policy-set --mode guard
python3 tools/sync_rename.py policy-set --path /DAO --mode auto
```

Wrappers:

```bash
ydm-rename-policy
ydm-rename-policy-set guard
ydm-rename-policy-set auto /DAO
```

### Scheduled Job Contract

Add a single command that the Termux job can call:

```bash
python3 tools/sync_rename.py preflight --local-root /sdcard/Download/ya_disk --format json
```

Output contract:

```json
{
  "schema": "ydm_rename_preflight:v1",
  "mode": "guard",
  "decision": "allow_bisync|skip_bisync|block_bisync",
  "reason": "no_candidates|high_only|auto_applied|ambiguous_candidates|error",
  "summary": {"high": 0, "review": 0, "blocked": 0, "applied": 0},
  "notify": true
}
```

Termux job logic:

```bash
preflight_json=$(python3 tools/sync_rename.py preflight --format json ...)
decision=$(printf '%s' "$preflight_json" | python3 -c '...')
case "$decision" in
  allow_bisync) python3 tools/sync_bisync.py run --apply ... ;;
  skip_bisync)  exit 0 ;;
  block_bisync) termux-notification ...; exit 0 ;;
  *)            python3 tools/sync_bisync.py run --apply ... ;;
esac
```

Failure policy:

- `observe`: fail-open to normal `bisync run`.
- `guard`/`auto`: fail-closed only when detector completed and found
  ambiguous candidates. If detector crashes before producing a decision,
  fail-open but log and notify.

### Status UX

Make `status` quiet by default:

```text
rename preflight: guard
last detect: 2 candidates, 1 high, 1 review
bisync decision: block_bisync ambiguous_candidates

AUTO  98cb0ecc DAO/a.txt -> DAO/b.txt     md5 match
HOLD  61fa...  DAO/x.txt -> DAO/y.txt     ambiguous_same_size_candidates

next: ydm-rename-status --show 61fa
```

Add:

```bash
python3 tools/sync_rename.py status --show CANDIDATE_ID
python3 tools/sync_rename.py status --verbose
python3 tools/sync_rename.py status --jsonl
```

Default `status` must not print old verbose `rename.log` entries. Show only:

- policy mode;
- last detect summary;
- current bisync state;
- current candidates;
- next action.

### Auto Apply Details

Implementation shape:

- Reuse existing `apply-detected` remote-only flow.
- Add `apply_candidates(candidates)` helper that:
  - validates all candidates before the first move;
  - rejects duplicate remote targets;
  - rejects more than `max_auto_candidates`;
  - performs `rclone moveto` for each candidate;
  - runs exactly one final `sync_bisync.py resync --apply`.
- If any remote move fails, stop immediately, log partial state, notify, and
  block further normal `bisync run` until `ydm-rename-status` is inspected.

Do not auto-apply:

- directories;
- `review` or `blocked` candidates;
- candidates under `download_only`;
- candidates where local new file disappeared after detection;
- candidates where remote old/new state changed since detection.

### Notifications

Use `termux-notification` when available:

```text
title: ydm rename preflight
content: blocked: 1 ambiguous candidate; run ydm-rename-status
```

Notification events:

- guard blocked scheduled bisync;
- auto applied one or more candidates;
- auto failed after partial move;
- detector crashed in scheduled job.

Do not notify on routine zero-candidate observe/guard runs.

### Acceptance Criteria

- `observe` behavior remains backward compatible and fail-open.
- `guard` blocks scheduled `bisync run` only for review/blocked candidates.
- `auto` applies only high-confidence file candidates, then performs one
  final `resync`.
- `ydm-rename-status` is readable on a phone-width terminal.
- `sync_bisync.py status` shows `resync_needed: False` after auto applies.
- Termux scheduled job handles all decisions without requiring interactive
  input.

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
- Scheduled auto-apply is allowed only after the detector has its own
  confidence gates and root-level opt-in.
- If detection is ambiguous, prefer blocking scheduled `bisync` with a clear
  message over silently converting a possible rename into delete+upload.

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

Detector tests:

- Single file rename under `DAO`: detected as high-confidence candidate.
- Two identical files under the same root: ambiguous, not auto-applied.
- Content changed while renamed: review required.
- Rename across policy roots: rejected.
- Rename inside `download_only` path: ignored or reported as protected, never
  auto-applied.
- Remote target already exists: blocked.
- Dirty bisync baseline or held bisync lock: scheduled preflight refuses
  auto-apply.
- Directory rename with unchanged subtree: initially review-only.
- Directory rename with changed subtree: blocked/review required.

Scheduled job tests:

- `observe`: logs candidate and still allows normal bisync.
- `guard`: blocks bisync on ambiguous candidate and sends notification.
- `auto`: applies one high-confidence candidate, runs `resync`, and leaves
  `resync_needed: False`.
- Failure after local scan but before remote move: no local/cloud mutation.
- Failure after remote move: logs recovery state and blocks further bisync
  until inspected.

## Backlog

- [x] Implement `tools/sync_rename.py plan|apply`.
- [x] Measure `rclone moveto` latency on Yandex.
- [x] Decide whether follow-up should be `bisync run` or `bisync resync`
      (`resync`; ordinary `run` creates conflict copies after out-of-band
      local+remote move).
- [x] Add `var/rename.log`.
- [x] Add `ydm-rename`, `ydm-rename-detect`, `ydm-rename-status`, and
      `ydm-rename-apply` wrappers.
- [x] Add `sync_rename.py detect` with current-local vs previous-local scan
      comparison.
- [x] Add candidate ids and `var/rename_candidates.jsonl`.
- [x] Add `apply-detected` for one reviewed candidate.
- [x] Add scheduled-job preflight in `observe` mode.
- [ ] Add `var/rename_policy.json` with default/root mode config.
- [ ] Add `policy-status` and `policy-set` commands.
- [ ] Add root-level policy mode resolution for detect/preflight.
- [ ] Add `preflight` command with `allow_bisync|skip_bisync|block_bisync`
      decision contract.
- [ ] Make `status` quiet by default; add `--show` and `--verbose`.
- [ ] Add scheduled-job preflight `guard` mode with Termux notification.
- [ ] Update Termux scheduled job to consume the `preflight` decision instead
      of ignoring detect output.
- [ ] Add scheduled-job preflight `auto` mode for high-confidence file
      candidates only.
- [ ] Add batch auto-apply helper with one final `resync`.
- [ ] Add partial-failure state and recovery guidance for auto mode.
- [ ] Add `ydm-rename-policy*` wrappers.
- [ ] Add ambiguity handling and narrow-screen `ydm-rename-status` output.
- [ ] Add directory rename detection as review-only.
- [ ] Validate whether Termux/Android shared storage emits usable file watcher
      events; if yes, add optional watcher as a latency optimization.
- [ ] Add bulk plan format and `plan-bulk|apply-bulk`.
- [ ] Add direct Yandex API backend fallback if `rclone moveto` is insufficient.
- [ ] Add Android normalization plan for risky `download_only` paths.
