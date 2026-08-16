# Changelog

Notable changes to this project, newest first. This project doesn't tag
releases, so entries are grouped by date. Detailed design/acceptance logs
for larger workstreams live in their own docs (linked below) — this file
is a scannable index, not a copy of them.

## 2026-08-16 — A partial scan could become the composite base

`find_last_full_scan()` picked the largest successful cloud scan of the last
two days, without checking that it covered the disk at all. Scan `93` is a
`--path /Books` scan: 13 893 files, the largest recent one, and it became the
**base** of the composite snapshot. Everything outside `/Books` and the
folder patches then did not exist as far as the snapshot was concerned —
`sync_tree --path /` showed a single child, and `report diff` was reading the
same distorted picture.

- `Analyzer.scan_covers_root()`: a scan qualifies as a base only if it has
  rows at the root (`parent_path` `''`/`'/'`). A scan started at `/Books`
  writes none, so it can only ever be a folder update.
- `find_last_root_scan()` replaces "just take the newest cloud scan" in both
  fallbacks — an old full scan is a valid base, a fresh partial one is not.
  Here the base went back to scan `72` (2026-03-04, 32 top-level folders)
  with 149 folder updates on top, which is how the composite is meant to work.
  No rescan needed.
- `tests/test_analyzer.py`'s `make_full_scan()` fixture wrote no root rows,
  so its "full" scans were partial by production's definition. It now seeds
  them, and a new test class pins the rule.

Cheap change detection — so the composite stops having to guess which folders
went stale — is designed in [`tasks/delta_scan/README.md`](tasks/delta_scan/README.md),
with the API costs measured.

## 2026-08-16 — The daemon backend described in its own terms

Working through the `tasks/sync_unification` manual verification checklist on
the live daemon host surfaced three places where rclone-bisync concepts were
applied to the daemon, where they mean nothing:

- **`sync_tree` showed every synced folder as `[L]` (local orphan).** The
  daemon's policy is a blacklist — it holds only `disabled` entries — but the
  tree built membership from the *whitelist* of `bidirectional` paths, which
  on a daemon host is always empty. It now derives the exclude set and uses
  the blacklist status pass, and `PolicyContext.blacklist_semantics` makes a
  path bidirectional unless it or an ancestor is disabled. `/pro` renders as
  `[B] 100%`, `/video` as `[B~] 5.5%`, excluded folders as `[X]`.
- **The menu reported `Status: NEEDS RESYNC`, `last bisync: never` and a lock
  on the daemon backend**, none of which exist there (`run_resync` raises
  `NotSupportedError`). `load_status()` now asks the daemon and shows its own
  state plus the exclusion count; the two menu entries named after bisync say
  what they do on the daemon. A dead `policy_status_payload()` call whose
  result was discarded is gone.
- `MenuConfig` carries `exclude_config` instead of three copies of a
  hardcoded `~/.config/yandex-disk/config.cfg`, honouring `YDM_EXCLUDE_CONFIG`.
- `sync_tree --format text` printed the whole `folder_updates` dict — 139
  entries on one line here, pushing the tree off screen. It now prints a count
  plus the first three, with the full mapping still in `--format json`.

## 2026-08-16 — Yandex Disk Trash restore tooling

`tools/trash_scan.py` scans a Trash subtree into a YDM-style SQLite database,
compares it against a `monitor.db` snapshot, and restores it whole or file by
file. Written during the `/Books` recovery; the notes below are what it took
to make it a permanent tool rather than a one-incident script. Full log:
[`docs/incidents/yandex-books-restore-2026-08-15.md`](docs/incidents/yandex-books-restore-2026-08-15.md).

- **HTTP 202 is no longer recorded as a completed restore.** `restore-files`
  now reads the operation href, polls `/operations/<id>` and stores
  `success`/`failed`/`accepted`. Since `restore-plan` retires an entry only on
  a `success` row, the old behavior meant a file whose operation later
  reported `failed` — which happened during this very incident — was skipped
  forever. New `poll-ops` resolves rows left at `accepted`.
- **`--trash-root` and `--restore-root` are required arguments.** They used to
  default to the August 2026 trash resource and `/Books`, so a bare
  `restore-root --apply --yes RESTORE_ROOT` was a mutating command aimed at
  one specific incident.
- **The token comes from `.env` (`YANDEX_DISK_TOKEN`) first**, as everywhere
  else in the project; `--token-source rclone` still reads `rclone.conf`.
  rclone refreshes its `access_token`, so a stale copy 401s with no
  explanation.
- **`compare-monitor` reports the metrics the incident write-up quotes** —
  `matched_files`, `size_mismatch_matched_files`, `md5_mismatch_matched_files`
  plus a mismatch sample. They previously came from ad-hoc SQL and could not
  be reproduced with the committed tool.
- `tests/test_trash_scan.py` covers all of it offline against a fake API
  client.

## 2026-08-16 — Ancestor-sibling coercion fixed, on a test bench this time

`tests/test_sync_policy_daemon.py` is the environment the previous entry's
incident lacked: a synthetic `monitor.db`, a throwaway `config.cfg` and a
patched `stop_start_daemon`, so the daemon-backend policy path can be
exercised without the live config, the daemon, or the cloud in reach. It
immediately found the remaining half of the original bug:

- **`_policy_coerce_for_daemon()` only excluded siblings one level deep.**
  Including `/Books/Math/АнГем` dropped `Books` from `exclude-dirs` and
  excluded `Books/*` except `Math` — but nothing under `Books/Math`, so the
  daemon would still pull down the whole of `Books/Math`. It now walks every
  level from the removed ancestor to the target. Verified against the real
  snapshot in dry-run: 55 → 97 exclude entries, 24 siblings at `Books/` plus
  19 at `Books/Math/`, with `/Books/Math/АнГем` the only addition to sync.
- **Coercion without a snapshot of an intermediate level now refuses.** It
  used to log a warning, drop the ancestor anyway and add no siblings — which
  hands the entire branch to the daemon. It raises `PolicyCoercionError` and
  leaves the policy file untouched; `add` reports it as a normal error.
- **`apply_policy()` refuses to clear a non-empty `exclude-dirs`** when the
  policy has no disabled paths — the state you get from running `add --apply`
  before `migrate`. Dry-run reports it as `clears_exclude_dirs` instead.
- **`ydm_menu.py` crashed on a host with no backend.** `MenuConfig.from_env_and_args()`
  let `detect_backend()`'s `BackendError` escape, so a machine without the
  daemon *and* without an rclone remote got a traceback instead of a menu —
  which is also why CI has been red on master since 2026-08-08. It now reports
  `Backend: none available` with the reason in the header, and the REPL catches
  `BackendError` from an action instead of dying.
- **`ydm_menu.py orphans` on a machine with no `monitor.db`** (a fresh
  checkout — the DB is gitignored) died with
  `sqlite3.OperationalError: no such table: scans`. It now checks for a
  successful cloud scan first and says what to run. The test that covered it
  read the developer's own `monitor.db`, so it only ever passed locally; it
  now seeds its own.
- `tests/test_sync_backends.py` never ran the way CI invokes it
  (`python tests/test_sync_backends.py`): it imports `tools.*` without putting
  the repo root on `sys.path`. Added the same bootstrap `test_sync_tree.py`
  already had.

## 2026-08-15 — Deletion guard for the daemon backend, after a 113 GB near-loss

The work below was validated against the **live** `yandex-disk` daemon and
the real `~/.config/yandex-disk/config.cfg`. A bug in the ancestor-sibling
coercion left `Books` out of `exclude-dirs`; its local copy was then deleted
while the daemon still tracked it, and on the next start the daemon read that
absence as a user deletion and propagated it to the cloud — 13 893 files
(113,6 GB) went to the trash. The data was restored and verified from the
snapshot database: 0 files lost against the last full pre-incident scan.
Root cause and timeline:
[`docs/incidents/yandex-books-delete-2026-08-14.md`](docs/incidents/yandex-books-delete-2026-08-14.md).

- `DaemonBackend.deletion_risk_paths()`: `apply_policy()` now refuses to
  restart the daemon while a path stays inside its scope but has no local
  copy — the exact precondition for a cloud deletion. A path that is only
  now leaving `exclude-dirs` is not flagged: the daemon downloads it rather
  than deleting it. Override with `force_unsafe=True`; dry-run reports the
  risk in `deletion_risk_paths` instead of raising.
- Backend selection no longer parses the human-readable name: callers use
  `SyncBackend.kind`. Previously `_resolve_backend_name()` compared
  `"yandex-disk daemon".split()[0]` against `"daemon"`, so under the default
  `--backend auto` the daemon-specific paths in `sync_policy.py` (including
  `migrate`) never ran.
- `ydm_menu.py --plain` reaches `MenuConfig` again; the flag was parsed but
  dropped, leaving `plain` hardcoded to `False`.

## 2026-08-14 — Unified sync interface across daemon and rclone backends

Added `tools/sync_backends.py`, a backend abstraction that lets the same
CLI work on Ubuntu with the official `yandex-disk` daemon and on
Android/Termux with `rclone bisync`:

- `DaemonBackend` applies `var/sync_policy.json` to `exclude-dirs=` in
  `~/.config/yandex-disk/config.cfg` and restarts the daemon.
- `RcloneBackend` writes `.bisync.filters` / `.download.filters` and runs
  `rclone bisync` as before.
- Auto-detection prefers the daemon when available; explicit
  `--backend daemon|rclone|auto` and `YDM_BACKEND` override it.
- `tools/sync_policy.py` gained `--backend daemon|rclone|auto` and
  `migrate --backend daemon` to import existing `exclude-dirs` into policy.
- `tools/sync_tree.py` now shows policy markers `[B]`/`[D]`/`[L]`/`[X]`
  for the daemon backend too, not only for rclone.
- `tools/ydm_menu.py`, `ydm_menu_config.py`, `ydm_menu_actions.py`,
  `ydm_menu_status.py`, and `ydm_menu_screens.py` are now backend-agnostic.
- `.bashrc` aliases `ydm-sync-add` / `ydm-sync-rm` now use policy-first
  semantics (add = include in sync, rm = exclude from sync) via
  `sync_policy.py`.
- `ydm_config.json` accepts an optional `"backend": "auto"` profile key.

Docs: [`tasks/sync_unification/README.md`](tasks/sync_unification/README.md),
[`tasks/sync_unification/DESIGN.md`](tasks/sync_unification/DESIGN.md),
[`tasks/sync_unification/HOW_TO_USE.md`](tasks/sync_unification/HOW_TO_USE.md).

## 2026-07-13 — Two `build_composite_scan()` bugs, found while writing unit tests

Added `tests/test_analyzer.py` (stdlib `unittest`, no new dependency) —
synthetic-data coverage for `Analyzer`/`StorageManager` that CI didn't
have before. Writing it against real behavior surfaced two bugs, both
fixed:

- **Nested-folder-conflict resolution dropped legitimate parent-folder
  updates.** When a composite scan's `folder_updates` contained both a
  folder and a more specific nested folder (e.g. `/A` and `/A/B`), the
  code treated this as a conflict and discarded the parent entry
  entirely — files directly in `/A` silently kept the stale base-scan
  version even though a newer partial scan had touched `/A` too. In
  reality there's no conflict: `_compare_composite_scan` matches on exact
  `parent_path`, so `/A` and `/A/B` are independent keys covering
  disjoint files. The whole "nested folder conflict" step was removed.
- **The composite-scan cache was never populated for the "no partial
  scans yet" result** — an early `return` skipped the cache-store code
  at the bottom of the function, so that specific (empty) result was
  silently recomputed on every call regardless of `use_cache`. Fixed by
  routing every outcome through a single cache-then-return path (which
  also removed an accidental verbatim duplicate of the cache-store code).

## 2026-07-12 — OSS readiness

Cleaned up the repository for a public release: removed personal example
paths, added standard OSS hygiene files (`CONTRIBUTING.md`,
`CODE_OF_CONDUCT.md`, `SECURITY.md`, issue/PR templates), split `README.md`
into an English primary version + `README.ru.md`, and consolidated ~50
files of AI-assisted debugging/planning notes into this changelog and a
few targeted doc updates (see this entry's own history for what got
removed). Full rationale and remaining backlog:
[`tasks/oss_readiness/README.md`](tasks/oss_readiness/README.md).

## 2026-07-10 to 2026-07-12 — Bidirectional sync via `rclone bisync`

Added an optional bidirectional sync mode (`tools/sync_bisync.py`) for the
`--backend rclone` path: new local files upload, deletions propagate both
ways, guarded by an access-health check, an explicit delete cap, a
resync-required gate when filters change, and process locking. Triggered
periodically via Android's `termux-job-scheduler` on the one device this
was built for. Full design, safety rationale, and verification log:
[`tasks/rclone_backend/README.md`](tasks/rclone_backend/README.md) (Этап 7).

## 2026-01-10 to 2026-01-11 — `rclone` backend

Added `--backend {api,rclone}`: an alternative to the official
`yandex-disk` daemon (which only ships for amd64/i386 and doesn't run on
arm64) using `rclone` as the cloud transport. New `CloudResourceClient`
abstraction (`YandexClient`/`RcloneClient`) feeds the same
`StorageManager`/`Analyzer` unchanged; new `tools/sync_filters.py`
(filter-file based sync management, no daemon) and `--backend rclone`
support in `tasks/junk/run_cleanup.py`. Full stage-by-stage log:
[`tasks/rclone_backend/README.md`](tasks/rclone_backend/README.md).

## 2026-01-06 — Fixed scans left `in_progress` despite finishing

Scans that actually completed successfully were sometimes left in
`started` status with folders stuck `in_progress`, because
`update_folder_status(..., 'completed')` was called outside the main scan
loop and `checkpoint_to_disk`'s `INSERT OR IGNORE` didn't update existing
scan-status rows. Fixed by moving folder-completion inside the loop,
switching to `INSERT ... ON CONFLICT DO UPDATE` for scan status, and
forcing a checkpoint after `finish_scan()`.

## 2026-01-06 — Fixed exponential duplicate rows on checkpoint

`checkpoint_to_disk` re-copied the entire in-memory file buffer on every
checkpoint instead of only new rows, causing exponential duplication
(some files ended up with 160+ duplicate rows). Fixed with a unique index
on `files` plus `INSERT OR IGNORE`; added a `report clean-duplicates`
command to clean up databases affected by the old behavior.

## 2026-01-06 — Concurrent-scan protection

A second `scan cloud` invocation while one was already running would
delete the shared tmpfs database out from under the first scan, crashing
it. Fixed with three layers: a PID lock file (`/tmp/ydm_cloud_scan.lock`,
self-healing if the holding process is dead), auto-recovery if the tmpfs
schema goes missing mid-scan, and process-specific tmpfs DB paths
(`/dev/shm/ydm_scan_<pid>.db`).

## 2026-01-05 — Fixed crash/interrupt data loss

A killed or crashed scan could lose all progress made since the last
checkpoint (checkpoints were infrequent — every 5 minutes / 10,000 files).
Added SIGTERM/SIGUSR1 graceful-shutdown handling, crash detection on
startup (`recover_crashed_scans`), and new diagnostic commands:
`report long-paths`, `report analyze-scan`, `report duplicates`.

## Earlier

Initial `ydm.py` core: cloud/local scanning against the Yandex Disk REST
API, resumable checkpointed scans on tmpfs, `report status`/`diff` and
friends. See `docs/PROJECT_YD_MONITOR.md` and `docs/ARCHITECTURE.md` for
the full design.
