# Changelog

Notable changes to this project, newest first. This project doesn't tag
releases, so entries are grouped by date. Detailed design/acceptance logs
for larger workstreams live in their own docs (linked below) — this file
is a scannable index, not a copy of them.

## 2026-09-12 — accepting the resync the menu offers used to crash

Found by the device's owner walking the Phase 14 screen end to end, which is
also the first time anyone accepted that prompt. `AttributeError: 'Namespace'
object has no attribute 'force_filter'` — after the policy and the filters had
been written and before the baseline was, so the add was half-applied and every
scheduled run would refuse until someone resynced by hand.

- **Not new: on master since 2026-08-25.** `sync_bisync resync` grew
  `--force-filter` that day and `_bisync_ns`, which hand-builds the Namespace
  the menu passes in, was not updated. Reproduced on `master` to be sure.
- **The branch had no coverage, by construction.** Every existing check of that
  screen stops before the resync — answering "no", or adding download-only,
  which never offers one. So the tests passed while the path crashed.
- **Defaults come from the parser now**, via a `build_parser()` split out of
  `parse_args()`. Restating them is what drifted; `parse_args([command])` cannot.
- **Two tests that would have caught it**, both verified against the bug: a unit
  check that each command gets every option its own parser defines, and the
  whole screen driven to the end against the bench's fake cloud — a real
  `rclone bisync --resync`, no network. It asserts the recorded baseline rather
  than "it did not raise", because the half-applied state is the actual damage.

## 2026-09-12 — the tree could not see what was never uploaded

Phase 14, from the device's owner: a folder copied onto the phone —
`Books/Math/База2`, 166 files, 1.6 GB — did not appear in the menu screen
called **Add LOCAL folder to sync**. Not filtered out, not too deep: it could
not get there. See `tasks/ydm_menu/GAP.md` G9 and G10.

- **Nodes come from the cloud snapshot, so a folder with no cloud row had
  nothing to render it.** `[L]` in practice meant "in the cloud, not in the
  policy, on disk". The screen's own list is derived from that tree, which is
  why every one of its 32 rows showed `cloud files:` above zero. The tree was
  equally blind: `--path /Books/Math --depth 1` showed `База` and not `База2`.
- **Fixing only the menu was not available.**
  `test_the_orphan_list_agrees_with_the_tree` pins the two together on purpose,
  so that the menu never offers what the tree does not show. The change is in
  `build_tree`; the menu gets it as a consequence and the invariant is untouched.
- **One walk of the disk, not a listdir per node.** Phases 12-13 were spent
  making the walk cost nothing per node, and every syscall here costs ~0.2 ms
  under proot. Measured against the same snapshot: **4 queries before, 4 after**,
  3810 nodes to 3850, plus 0.28 s of filesystem walk once.
- **No tenth marker.** No cloud files, a directory on disk and no policy entry
  already resolve to `orphan` → `[L]` through `local_state()`, with no new
  branch anywhere.
- **The fuller list made the flat one worse, so the screen descends now.**
  `База2` brought its own 40 subfolders along — each just as local and just as
  absent from the policy — taking the list from 32 rows to 72. Trimming it would
  have broken the invariant above, and the tree is right to show them. So the
  presentation changed instead: the root is 4 rows, and the owner's folder is
  three keypresses away. A row can be both addable and a way in; `+39 inside` is
  information, since adding a folder covers everything under it.
- **The bench describes the case now** (`BenchPath.in_cloud`, `/fresh`), which
  makes the two derived bench checks cover it for free — the exact-set orphan
  list and the menu-agrees-with-tree invariant.

## 2026-09-09 — the test suite was notifying the phone

Running `python3 -m unittest discover -s tests` on the device sent a real
Android notification reading *"bisync blocked; run sync_rename.py status"* —
the guard's one actionable alarm, from a test. The owner saw it and reasonably
took it for a stopped sync. It was not: `var/bisync.log` said `run OK` on every
cycle and the live guard answered `allow_bisync (no_candidates)` all day.

- **PATH could not reach it.** `job_run.sh` calls Termux's binary by absolute
  path, which is right — Termux's bin is not on the container's PATH — but it
  left the notification the one effect in the script no test could stub. The
  binary is now named through `YDM_NOTIFY_BIN`.
- **Invisible on CI, by construction.** GitHub Actions has no
  `termux-notification`, so `[ -x ]` fails and the call is silently skipped.
  Only a device run can show this, which is the same blind spot as the eight
  rclone checks CI skips.
- **The stub turned a side effect into coverage.** Three new assertions say
  which verdicts reach the phone: a block does, and `skip_bisync` and
  `allow_bisync (error)` do not. A notification per skip would train the
  operator to swipe away the one that matters. None of this was tested before,
  and the block assertion can only pass while the override is in place — so it
  is also what keeps the suite quiet.

## 2026-08-29 — the guard points down, not up

`docs/ANDROID_SETUP.md` described the rename preflight as settling a
**cloud-side** rename. It is the other direction, and the doc had already
misled someone: issue #18 repeated the wrong framing as its own understanding
while noting, fairly, that the cloud path had not been exercised.

- **Nothing was left untested.** `find_candidates` diffs two local scans and
  never asks the cloud, so the code cannot tell where a rename came from — a
  test for the cloud path would be letter-for-letter the one that exists.
- **Three things in the code say local**: `bisync treats a local rename as
  delete+upload` at the top of `sync_rename.py`; on a match the guard runs
  `rclone moveto` *on the remote*, carrying the local rename upward; and
  `validate_auto_candidates` requires the old name to still exist in the cloud
  and the new one not to — the state right after a rename below.
- **What a cloud rename actually does** is now written down instead of
  implied: nothing locally, so the preflight is silent and bisync brings the
  new name down; on the next run that arrival looks like a local rename, where
  `auto` stops at `Remote target already exists` and `guard` calls it `high`
  and lets bisync through. Neither can lose data.

## 2026-08-29 — the guard's log lines, provoked rather than awaited

Documentation and one comment. The guard writes five kinds of line to
`var/bisync.log` and an ordinary run produces none of them, so on the device
they had only ever been *not seen* — eight `run OK` in a row proved the code
had arrived, not that it worked. Waiting for a real failure to find out is the
wrong order: that failure is issue #5, which cost hours of `run OK`. All five
were forced instead ([issue #18](https://github.com/kogriv/ydm/issues/18)),
and `docs/ANDROID_SETUP.md` now carries the recipe for each.

- **`baseline_unreadable` means VACUUM, not a stray writer.** `monitor.db` is
  WAL, where a writer does not block readers: 5.5 s of `BEGIN EXCLUSIVE`, past
  the 5 s connect timeout, went unnoticed. Only `PRAGMA locking_mode=EXCLUSIVE`
  produced the line — which in production means `VACUUM` or a checkpoint, i.e.
  `report prune --apply --vacuum`, step 3 of this same job. The comment in
  `sync_rename.py` said the opposite and would have sent a reader looking for
  the wrong thing.
- **Check the mode before reading the log.** Under `default_mode: observe`
  every decision is `allow_bisync (observe_mode)` and nothing is ever written,
  so an empty log proves nothing. The device runs `guard`.
- **The sixth case is the silent one**, now stated as such: a baseline present
  and nothing renamed writes no guard line at all.

## 2026-08-29 — the tree's freshness report was outside every scope

Phase 13.3, found on the device while confirming Phase 13. The device saw the
same win the author did on `orphans` (132 queries → 54), but the tree CLI only
went 260 → 182 and still opened 35 connections. The scope Phase 13 added lives
inside `select_snapshot_for_tree`; `snapshot_freshness()`, which `sync_tree.py`
calls afterwards to date the composite, builds a second one and was left
outside it.

- **32 of those 35 connections came from that one call.** `sync_tree.py`
  260 queries → 182 after Phase 13 → **98** now, and 35 connections → **4**.
  Wall clock on the device: `--depth 3` 2.8-3.4 s → **1.8 s**, `--depth 4`
  → **2.4 s**, `--depth 5` → **2.6-2.9 s**.
- **It also silently disabled the Phase 13 cache.** No scope means nothing is
  remembered, so `SELECT scan_root, scan_depth` came back five times per scan
  in the tree while `orphans` was down to one.
- **Pinned at the CLI, not at a helper.** The unit tests could not see this:
  each one opens the scope itself, and the omission was in the caller. The
  new tests run `main()` and assert connections do not grow with the number of
  scans in the database — 10 scans and 40 must cost the same. Both fail on the
  code before this change (168 connections at 40 scans).
- **Output unchanged**, checked at depths 3, 4, 5 against `ca0b81b`. Note for
  anyone repeating it: `folder_updates` key order already varies between two
  runs of the *same* build, at `ca0b81b` too, so compare parsed JSON rather
  than bytes.

## 2026-08-29 — choosing a snapshot stops opening 180 connections

Phase 13, from an aside in the device's report ([issue
#16](https://github.com/kogriv/ydm/issues/16)) that reproduced here at the
author's scale. Phase 12 stopped the tree walk paying per node; what remained
does not scale with nodes at all — it scales with how many scans the database
holds, and was invisible next to the walk until the walk got cheap.

- **41% of an `orphans` run was opening connections.** 360 statements of 872,
  180 connections, all before the read scope the walk enters. Now 4 and 2.
  `orphans` costs 872 queries → **346**, the tree read path 872 → **346** at
  depth 4 and 5, 862 → **336** at depth 3. Output byte-identical against
  `ca0b81b` on all of them.
- **The same row, fetched by three readers.** `scan_covers_root()`,
  `scan_root_path()` and the retirement pass all want
  `SELECT scan_root, scan_depth FROM scans WHERE id = ?`; the device's report
  showed it four times per scan id. Fetched once per scan now.
- **The read scope lives inside `select_snapshot_for_tree`**, not in its four
  callers, so they cannot disagree about whether it is needed. It nests
  harmlessly in the scope the walk already opens.
- **The cache is keyed to the scope, not to the `Analyzer`.** The first
  version remembered for the object's lifetime, which would have been a wrong
  answer rather than a slow one: a write between two reads would have gone
  unseen. `StorageManager.read_scope` hands out a scope *number* rather than a
  flag, so a cache filled in one scope is never trusted in the next; nested
  scopes share the outermost number, because they are one read. Pinned by a
  test where a scan gains its root row between two scopes and has to change
  its answer.
- **Stopped short of the next one deliberately.** The top of the report is now
  `SELECT DISTINCT parent_path … type='file'`, twice per scan. The gain
  shrinks from here and the work grows, and it is worth measuring on the
  device before touching.

## 2026-08-28 — one add, one daemon restart

Phase 5.1: `ydm-sync-add` was a second implementation of "add this path", and
it had drifted.

- **Two commands where the menu runs one.** The alias ran `sync_policy.py add
  --apply` and then `render-filters --apply`. Both of those apply the whole
  policy to the backend, so a single add stopped and started the daemon
  **twice**. The policy file came out identical either way, which is why
  nothing noticed; on a 1.5 TB disk the second restart is a re-index that buys
  nothing. Counted on the bench: 2 restarts against `action_add`'s 1.
- **`ydm_menu.py add` is now the one entry point**, used by the alias and the
  menu. It takes `--path`, `--mode`, `--force-risk`, and emits JSON.
- **It is dry by default**, which the alias never had — `tools/aliases.sh` said
  so in as many words and pointed at `sync_policy.py` for a preview. Without
  `--apply` it prints the same delta the menu shows before asking, and writes
  nothing.
- **The dropped `render-filters` step is not a loss.** On rclone that is
  exactly what `RcloneBackend.apply_policy` does, so routing through the shared
  path keeps it; on the daemon it wrote filter files the daemon never reads.
  Pinned by a test either way.
- **The first theory was wrong and the bench said so.** `add_policy_path()`
  writes only the policy file, which read like the alias never applying to the
  daemon at all — a much worse defect. It does apply: `sync_policy.py`'s
  command handler calls `_apply_backend_policy` after the write. The bench was
  what settled it, before any of this was written down.

## 2026-08-28 — and the counting half asked them too

Phase 12.2 and 12.3, closing the phase. `_folder_file_counts` and
`_count_files_for_prefix` were already index seeks rather than table scans —
that was Phase 9 — but `apply_sync_percent` asked them once per node, and the
composite asked again for every scan serving an update beneath that node.

- **A count per folder is a property of the scan.** `FolderFileCounts` reads
  all of them with one `GROUP BY`, then answers both questions from a sorted
  key list and running totals: "the counts in this subtree" is a slice, "files
  at and under this prefix" is a difference of two totals. The slice starts at
  `<base>/` and not at `<base>`, for the reason it does in SQL and in
  `folder_updates_under` — `/Books/Math-old` sorts between `/Books/Math` and
  `/Books/Math0` and is not a descendant.
- **Measured on the author's snapshot**, walk and counts together, inside
  `reuse_connection()` as the CLI runs them: depth 4 **39 319 queries → 125**,
  depth 5 **53 820 → 125**, and the count no longer grows with depth.
  `ydm_menu orphans` **54 568 → 873**. Output identical in every case.
- **The first reading of those numbers was four times too high**, because the
  harness called `apply_sync_percent` outside `reuse_connection()` and every
  node then opened its own connection — two PRAGMAs each, 15 240 of them,
  swamping what was being measured. The budget tests now wrap it the way the
  CLI does.
- **`sync_tree`'s own wall clock moves less than the query count**, because a
  CLI run also takes a local scan, and those inserts are most of what is left.
  Unrelated to this, and not something to expect back.

## 2026-08-28 — the tree asked the same questions once per node

Phase 12.1 and 12.5. What the device's profile found once 9.3 turned out not
to exist (issue #13): PR #4 and Phase 9 made a query cheap, and nobody had
touched how many there were — 50 813 for 3 801 nodes, 13.4 per node, 67% of
the run.

- **Which folders a scan holds is a property of the scan, not of the node
  asking.** `fetch_child_names` answered per node and cost up to ten queries
  each: one for `type='dir'` rows, four inferring names from file paths under
  both spellings of `parent_path`, and five more in the fallback loop when the
  folder had neither. `ChildIndex` reads it whole in two queries per scan — the
  dir rows, and the distinct `parent_path` of every file, since a folder named
  in a path is a folder that exists. Built lazily per `scan_id`, so a walk that
  never enters a subtree never pays for the scan behind it.
- **Measured on the author's snapshot** (144 MB, 3 810 nodes at depth 5):
  `sync_tree --depth 4` 19 882 queries → 84; `--depth 5` 27 707 → 84;
  `ydm_menu orphans` 54 568 → 26 945. The remainder is the counting half,
  which is 12.2 and 12.3.
- **Output is unchanged**, verified byte for byte at depths 3, 4 and 5 against
  the same snapshot, after the `folder_updates` key order is normalized — it
  floats between runs, as Phase 9 already recorded.
- **Two blind spots at the root, found by building the index and fixed on both
  paths.** Root is the one folder answered by extracting a first segment
  rather than by a range, and that query carried
  `parent_path NOT LIKE '/%/%/%'` — so a top-level folder whose files all sit
  three or more levels down was invisible there, and since the walk descends
  into what it lists, the whole subtree went missing, which in `orphans` reads
  as a local copy with no cloud counterpart. It also did `substr(parent_path,
  2)`, assuming a leading slash, so a scan that wrote `Books/Math` produced no
  root listing at all. Both are reachable only when the scan has no dir row at
  the root, which is why the live snapshot renders identically either way.
  Fixed in `infer_dirs_from_files` rather than reproduced in the index:
  leaving them would have meant the tree and the menu's cloud listing
  answering the same question differently, which is its own defect. Dropping
  the depth bound costs nothing — LIKE is case-insensitive by default, so
  neither form was ever an index range and both read every row of the scan.
- **`tests/query_count.py` is new.** Wall-clock cannot police a query count
  from this machine — it is 5-15x faster than the device, so a real
  improvement hides in the noise. A query count is the same number on both,
  and it is the number the device pays for. Two tests fence the walk: it must
  not cost per node, and depth 4 must cost what depth 3 does.

## 2026-08-28 — the rename guard was comparing whatever it found

Found on the Android device while verifying that the guard still worked after
the five merges — the verification itself triggered it. Issue #14.

- **The baseline was "the newest local scan", not "the newest scan of this
  mirror."** `sync_rename` diffs the two most recent successful local scans to
  find renames, and never checked what tree either one covered. One preflight
  run against a different `--local-root` left a scan of another directory at
  the top of the table, and the next run read the difference between two
  unrelated trees as renames: two `blocked` candidates pointing at
  `/RCLONE_TEST` — the bisync check-access sentinel, matched by size — and a
  `block_bisync` that stopped the scheduled sync for a cycle.
- **The column to fix it with had been added and never written.** PR #10 added
  `scans.scan_root`; `ydm.py scan local` fills it, and `run_local_scan()` — the
  path every tool takes, including the preflight — did not. All 93 local scans
  on the device carried NULL. It records a normalized root now, and the
  baseline query selects on it.
- **NULL is not treated as a match.** Letting an unattributed scan stand in for
  any mirror would have kept the defect alive in every database that already
  existed, which at that point was all of them. The cost is one skipped cycle
  the first time, after which there is an attributed scan to compare against.
- **No baseline now means skip, not allow.** That path used to return early
  with an error and `allow_bisync` — fail-open, and permanently so: returning
  *before* taking the scan meant the next run had no baseline either. The scan
  is taken first, and of the two honest answers to "nothing to compare against",
  only "do not run bisync" is safe. `observe` mode still allows.
- **`cmd_preflight` recomputes the verdict from the candidate list**, so the
  reason is carried across explicitly. An empty list there means "nothing
  changed", which is not what "nothing could be compared" means, and collapsing
  the two would have restored the fail-open one layer up.
- **Guard decisions reach the log.** Only `sync_bisync.py` wrote to
  `var/bisync.log`, so `skip_bisync` and `block_bisync` left no trace at all:
  `run OK` lines, a gap, `run OK` again, with nothing to distinguish the guard
  stopping a run from the job never firing. The Termux notification is not a
  substitute — it is not kept.
- **`tests/test_sync_rename.py` is new**: the module had no tests whatsoever.
  17 of them, none touching rclone. Six mutations, each killed.

Found in review of the above, and fixed with it:

- **"Cannot read the database" was being reported as "no scan of this
  mirror."** The baseline query answered every `sqlite3.OperationalError` with
  "no baseline", so a damaged or unopenable database produced
  `skip_bisync (no_comparable_baseline)` — a log line that reads as "you passed
  the wrong `--local-root`" and sends whoever is diagnosing it to the wrong
  place. Only the missing column means that now; anything else raises and the
  caller answers it under `baseline_unreadable`. Same verdict, both skip; the
  point of the new log line is that the reason is true.
- **The one branch that runs bisync *without* a guard still logged nothing.**
  `job_run.sh` gained lines for `skip_bisync` and `block_bisync`, which stop
  the sync — but an error inside the preflight travels in the JSON envelope
  with exit code 0, so it is not caught by the `FAILED rc=` test and ends in
  `allow_bisync (error)`: bisync runs, unguarded, looking exactly like a clean
  run. That is issue #5 verbatim, and it is now written down.
- **`var/bisync.log` is resolved the way the rest of the codebase resolves
  it.** The script wrote a bare relative `var/bisync.log` while
  `sync_bisync.py` goes through `var_path()`, which `YDM_VAR_DIR` redirects —
  so anything setting it would have split the log in two.
- **`tests/test_job_run.py` is new**: the scheduled job decides every half hour
  whether bisync runs and had no coverage at all. 7 tests, driven through a
  stub `python3` on PATH, reaching neither rclone nor the device. Seven further
  mutations, each killed.

## 2026-08-27 — one sentinel, two meanings, and a warning that never fired

Phase 11. `tests/test_ydm_menu.py` named `ROOT/monitor.db` and
`ROOT/var/sync_policy.json` outright and let the backend auto-detect, so what
those checks asserted depended on the machine running them. Fixing that was
meant to be mechanical. It was not: a probe written to prove the isolation
found the live daemon config still being opened, and the caller was the
product.

- **`snapshot_freshness(exclude_dirs=None)` loaded the daemon's `exclude-dirs`
  from the *default* path.** The menu passes no list under rclone on purpose —
  a whitelist backend is not governed by the daemon's blacklist — and got the
  daemon's exclusions applied to its numbers anyway. Folders rclone does
  compare were counted as "never compared", which moves them into the bucket
  that never warns. The failure direction is the bad one: a stale snapshot
  stayed quiet. No list now means nothing is excluded; callers that want the
  daemon's list ask for it.
- **`get_diff()` did the same thing**, with no parameter to say otherwise, and
  the menu's `d` screen inherited it. It gained an `exclude_dirs` argument;
  omitted, it behaves exactly as before, which is right for the CLI.
- **One definition for both menu readers.** `menu_exclude_dirs(cfg)` — the
  daemon's list from the configured file, or an empty set. Two readers
  deriving this separately is how they drift apart.
- **A mutation survived and had to be answered.** Removing the explicit list
  from `sync_tree`'s freshness call broke nothing in the suite, although it
  would have silently changed the stale/excluded split on the live machine.
  Now covered by a bench that puts an `exclude-dirs` file in its own home
  directory.
- **The probe is kept**: `tests/probe_live_state.py`, outside CI. It hooks
  `open` and `sqlite3.connect`, prints the stack for every touch of
  `monitor.db`, `var/sync_policy.json` or `~/.config/yandex-disk/config.cfg`,
  and exits non-zero. Nine modules, zero touches.
- Also: four `MenuConfig` constructions in the bench were missing
  `exclude_config` and so read the live one, and `test_from_env` asserted that
  `db_path` ends in "monitor.db" — true of the default too, so the argument
  could have been ignored entirely.

- **`--exclude-config` now exists.** `sync_tree` read
  `getattr(args, "exclude_config", None)` in three places and got None every
  time, because the flag had never been declared — so it resolved the daemon's
  default path however it was invoked, and on the bench only an overridden
  HOME kept that off the operator's own file. The menu had the field in
  `MenuConfig` and the `YDM_EXCLUDE_CONFIG` variable, but no flag to fill it
  and nothing to hand its `sync_tree` child. Both have it now, same name and
  default as `sync_policy.py`.

Two of the four mutations on that flag survived the first pass. One of them
took some finding out: the config-driven membership cannot be observed through
markers at all — with the policy switched off there is no overlay, so every
node renders `[?]` whatever the exclusions are. What is observable is the
`config_path` the run reports, and only the v1 schema reports it.

Header on the live daemon before and after: `base #72, 176 day(s) old, serves
48.0% of files`. 339 tests → 352.

## 2026-08-27 — the menu entry that did the opposite of its label

Phase 10. Menu 4 read "Remove folder from sync" and, on the daemon, listed
the 52 folders that were **excluded**. Picking one deleted its policy entry,
which under a blacklist means the folder leaves `exclude-dirs` and starts
syncing. `Books` — 113 GB, the folder the 2026-08-14 incident emptied — sat
second in that list.

- **The screen now depends on the backend, like items 5 and 6 already did.**
  On the daemon it is "Stop syncing a folder (exclude)": it lists what is
  actually syncing and adds an exclusion. Excluding is the one direction that
  is safe by construction here — it takes a folder out of the daemon's reach
  and cannot delete anything. The opposite direction stays in menu 2, which
  shows the `no longer excluded` line first. On rclone nothing changed:
  there a policy entry does mean "synced".
- **No data was ever at risk on this machine**, and that is luck rather than
  design: the fatal case is a folder missing locally, and `deletion_risk_paths`
  refuses the daemon restart for exactly that. But 16 of the 52 exclusions do
  exist locally, and for those no guard fires — the menu would simply have
  started downloading them.
- **The bench could not have caught it.** Its policy is rclone-shaped and
  holds all three modes, so `[X]` is one entry among many. A daemon policy is
  *nothing but* exclusions, and in that degenerate case the screen inverts.
  `make_daemon_policy()` is the missing case; the invariant on top of it is
  that no path through menu 4 may shrink the exclude list.
- **Four smaller things found while fixing it.** A locally created folder is
  inside the daemon's scope but absent from the snapshot, so the screen reads
  the disk as well. Excluding a parent silently kills `bidirectional` children
  in the policy, so they are named before the confirmation. Excluding below an
  existing exclusion is a no-op that used to report success. And the preview
  said "Adding /video as disabled" — the policy's vocabulary, not the
  operator's.
- **The local copy is not deleted.** The order — exclude, let the daemon
  restart, then delete — is the only safe one, and an irreversible step placed
  immediately after a daemon restart is the shape of the incident. The screen
  prints a quoted `rm -rf` and stops.
- **`monitor.db-wal` and `monitor.db-shm` were tracked** although `monitor.db`
  is ignored — committed by accident two days ago, and deleted again by the
  next clean close, so `git status` reported a deletion after every test run.
  Untracked and ignored.

318 tests → 339. Ten mutations, each killed; one of them first *hung* the
run instead of failing it, which is why the loop that drains the screen now
requires the list to shrink on every round.

## 2026-08-25 — a method that had never run, and the leak hiding behind it

Phase 9.5. `StorageManager` defined `get_connection` twice; Python keeps the
last, so the first had been dead code. It carried `PRAGMA synchronous`,
`PRAGMA journal_mode` and the whole "Level 2" tmpfs schema recovery — the
one written for a scan into `/dev/shm` whose file disappears. On every
machine that scans through tmpfs, which includes this one, that recovery
could not fire.

- **The two are now one.** The pragmas apply and the recovery is reachable.
  A structural test parses `ydm.py` and fails if any method of the class is
  defined twice, because nothing else can catch this: no warning, no error,
  and the edit simply lands in the copy that never runs. That is how it was
  found — a change made to the wrong `get_connection` had no effect at all.
- **Restoring the pragmas costs nothing.** Measured on the 138 MB database:
  connect and close without touching the file, 0.068 ms; connect plus one
  query, 0.522 ms; the same with both pragmas, 0.511 ms. `sqlite3.connect`
  is lazy, so the price is the first statement opening the file — which any
  first statement pays. **This corrects yesterday's note**, which put ~0.6 ms
  on the pragmas: that figure was read off the method that never ran. The
  `reuse_connection()` win stands — it removes the first touch, 8 504 times
  in a depth-4 walk — but the attribution inside it was wrong.
- **`init_db()` leaked its connection.** `with sqlite3.connect(...)` commits
  or rolls back; it does not close. The connection then survived until the
  next garbage collection, holding the file — enough for the very next
  `PRAGMA journal_mode` to fail with `database is locked`. That is not
  hypothetical here: the recovery calls `init_db()` and immediately reopens
  the file, so the leak stood directly in the way of the case being restored.
  Found because a test written for the recovery failed for this instead.

303 tests → 310, green under a CI simulation with no rclone and no
environment.

## 2026-08-25 — the tree stops being slow, and it was never where we looked

Phase 9 ([`tasks/ydm_menu/BACKLOG.md`](tasks/ydm_menu/BACKLOG.md)). Issue #3
named three leftovers after its index fix; profiling found that none of them
was the dominant cost, and the two that were had been named nowhere.

| | before | after |
|---|---|---|
| `sync_tree --depth 3` | 11.98 s | **1.12 s** |
| `sync_tree --depth 4` | 22.05 s | **1.15 s** |
| `sync_tree --depth 5` | 37.74 s | **1.37 s** |
| `ydm_menu orphans` | 120.94 s | **2.30 s** |

Measured on a snapshot of 75 510 rows and 5 506 directories — the same size as
the device's. JSON output is byte-identical in all four cases once
`folder_updates` key order is normalized.

- **Two lookups swept a 2 251-entry dict once per node.**
  `select_scan_id_for_path()` walked every key keeping the longest prefix
  match — 6.34 million `startswith` calls in a depth-4 walk. But the ancestors
  of a path need no search: they are the path, then its parent, and so on, so
  a handful of dict lookups answers it. `count_cloud_files_for_path()` did the
  mirror image looking for *descendants* — 9.38 million comparisons in one
  `orphans` run — and descendants are a contiguous slice of the sorted keys,
  found by binary search. Same boundary as PR #4's SQL range, and for the same
  reason: the slice starts at `<subtree>/`, or `/Books/Math-old` is counted as
  part of `/Books/Math`.
- **The `LIKE` PR #4 removed from one half was still in the other.**
  `_folder_file_counts()` and `_count_files_for_prefix()` filtered subtrees
  with `parent_path LIKE '<prefix>/%'`, which SQLite cannot fold into an index
  range while LIKE is case-insensitive — once per node this time, not once per
  folder. Now a range, and split into two statements: SQLite will not turn an
  `OR` into a single range seek, so keeping the folder's own row in the same
  query would have quietly kept the full scan.
- **One connection per walk instead of thousands.** `get_connection()` opened
  a fresh connection for every query — 8 504 in a depth-4 walk, 7 802 more in
  the counting that followed. `StorageManager.reuse_connection()` serves them
  all from one, and needs no changes at the call sites: the handle it returns
  ignores `close()` and resets `row_factory`, because every DB helper here
  closes in a `finally` and Analyzer sets `sqlite3.Row` on connections it
  opens. Reads only, and deliberately scoped to start after the local scan.
- **`_dir_size()` was left alone, on purpose.** Issue #3 listed it, and on
  this machine it is 0.22 s of an 11.8 s profile — the cost is `/sdcard`'s
  FUSE layer, which cannot be reproduced here. Changing it blind would be
  guessing; it stays filed as 9.3 with the device to measure it.
- **A method that has never run.** `StorageManager` defines `get_connection`
  twice and the second wins, so the pragmas in the first — `synchronous`,
  `journal_mode` — are never applied, and neither is the tmpfs schema-recovery
  block. Filed as 9.5 and fixed the same day; see the entry above.

## 2026-08-25 — the barrier goes where the damage came from

Phase 8's two open decisions, settled and built
([`tasks/ydm_menu/DESIGN-2026-08-24.md`](tasks/ydm_menu/DESIGN-2026-08-24.md),
"Решения 8.7 и 8.8"). 261 tests → 275.

- **Adding shows what it will change, then asks once.** Removal had three
  barriers and adding had none, although adding is what caused 2026-08-14.
  The design's first answer — confirm when the risk inspector sees a risk —
  is withdrawn: on 14.08 there was no risk to see, the path existed and was
  fully materialized, so a risk-gated prompt would have been silent exactly
  when it was needed. What speaks instead is the delta: `no longer excluded
  (starts syncing): Books`. The bench makes the sharper point — the count can
  go `1 -> 1` while the content inverts, so the names carry the warning and
  the number only frames it.
- **The preview runs the production path on a copy.** The policy is copied to
  a temp file, `add_policy_path(apply=True)` is applied there, and the result
  goes through `apply_policy(dry_run=True)`. That is deliberate: the daemon
  coercion that drops a disabled ancestor is the 14.08 logic itself, and
  computing the delta "by hand" would put a second copy of it beside the
  first. Neither the policy nor the daemon config is touched.
- **A refusal is reported before it happens.** `deletion_risk_paths` and
  `clears_exclude_dirs` are filled in on a dry run and *raised* on a real one,
  so the preview now warns rather than letting the refusal arrive after the
  person has already committed.
- **`t` shows the trash.** The one screen meant to be read under stress: what
  is missing when files have vanished is not courage but the hashed name —
  `trash_scan.py` wants `--trash-root trash:/Books_<hash>` and nobody knows
  theirs. The menu lists the entries with their origins and prints the two
  commands. Restoring stays in the CLI: it changes data and should say so.
- **`prune` deliberately gets no entry, and a line instead.** It is
  housekeeping wanted twice a year; an entry would only lengthen the list. But
  the database grows with every scan and nothing said so, so detailed status
  now reports size, scan count and what is droppable. Measured at 0.19 s on a
  138 MB, 53-scan database.
- **The bench now runs in CI.** `tests/test_sync_bench.py` — 74 checks, and
  the only coverage the menu screens have — ran nowhere but a developer's
  machine. That is how a menu offering paths from someone else's setup passed
  every green run. Its rclone-backed checks skip themselves where rclone is
  absent, so a runner without it still gets the other 66.

## 2026-08-25 — what running on the real device turned up

Four findings from the machine that actually runs this every 30 minutes, each
filed with measurements and fixed with a test. Three are defects that had been
in place for weeks; the fourth is the operator surface that never shipped.

> **Upgrading an existing database: run `python3 ydm.py init` once.**
> It is an `ALTER TABLE`, metadata only, seconds. Without it every scan started
> by a *tool* — `sync_rename.py`, `sync_tree.py`, `ydm_menu.py`,
> `sync_bisync.py` — dies with `table scans has no column named scan_root`,
> while `ydm.py` itself keeps working. That asymmetry is what hid it.

- **The schema migration ran on the one path that did not need it.**
  `_ensure_scan_scope_columns()` was reachable from `init_db()`,
  `_init_final_db()` and the tmpfs branch of `start_scan()` — every path
  `ydm.py` takes and none of the paths a tool takes, because
  `tools/sync_common.py` builds its StorageManager with
  `use_temp_storage=False` and such an object never calls an initializer.
  Worse than a crash: the scheduled job treats a failing rename preflight as
  `allow_bisync` on purpose, so **every scheduled bisync ran without the
  rename guard** while `var/bisync.log` kept printing `run OK`. Reads were
  never affected — `_recorded_scope()` already fell back to inference.
- **`sync_bisync` defaulted `--filter-path` to the download filter.** An
  incomplete migration, not a choice that aged badly: `752c182` split one
  filter into two and added `default_bisync_filter_path()`, but the three
  `args.filter_path or default_filter_path(...)` lines predate the split. So
  `status` hashed the download filter and compared it against a baseline
  recorded from the bidirectional one — `resync_needed: True` had been a false
  alarm for weeks. `resync` also gained the guard it lacked: `run` refuses a
  filter whose hash does not match the baseline, but `resync` is the command
  that *writes* the baseline, so it had nothing to compare against. It now
  refuses the two download sets by name, before reading the file and long
  before rclone, unless `--force-filter` says otherwise.
- **Tree child inference read every row of the snapshot, once per folder.**
  `parent_path LIKE '<prefix>/%'` cannot be folded into an index range while
  `LIKE` is case-insensitive, so `idx_files_unique` served only `scan_id=?`.
  A range over the same index — `['<prefix>/', '<prefix>0')`, and the leading
  slash matters, or `/Books/Math-old` gets swallowed — turned a walk that did
  not finish into one that does: depth 4 was killed at 460 s and takes 154 s,
  `ydm_menu orphans` was killed at 13 minutes and takes 420 s. Seven minutes
  is finished rather than fast; the remainder is measured and filed as
  [`tasks/ydm_menu/BACKLOG.md`](tasks/ydm_menu/BACKLOG.md) Phase 9.
- **`tools/aliases.sh` shipped a quarter of itself.** 126 lines against the
  463 in use: scanning and the tree had wrappers, bisync, the rename guard and
  the policy layer had none — everything except the part that changes data.
  Fourteen commands ported, plus `ydm-help --plain` and its pager. Two repairs
  on the way: `ydm-scan-cloud` was an `alias`, which a non-interactive shell
  does not expand at all, so for any script it simply did not exist; and the
  bisync filter is now derived when a command runs, not when the file is
  sourced, because `YDM_LOCAL_ROOT` is commonly exported after the source line.
- **`ydm` is the menu again, and `ydm-cli` is the CLI.** Three shipped
  documents said `ydm` meant the menu and the alias file rebound it to the
  CLI, so sourcing it took the menu away from whoever had been using it. The
  CLI keeps a wrapper it never had before.

## 2026-08-25 — the Android side stops being one person's phone

The device answered the three questions `tasks/android_verify/` was blocked
on, and answering them showed the documentation described an environment the
project would not run in. New: [`docs/ANDROID_SETUP.md`](docs/ANDROID_SETUP.md)
and `tools/termux/`.

- **It was never Termux.** YDM runs in a proot-Debian container inside Termux;
  the scheduler runs in Termux and reaches into the container. Both halves are
  now written down, with the bind-mount trap (`/root/notes` is a bind mount, so
  a bare `proot-distro login` has no repository at all) and the reason the job
  script cannot live in the repository — `termux-job-scheduler` resolves paths
  in Termux's filesystem, and shared storage silently ignores `chmod +x`.
- **The scheduled job ships.** `tools/termux/job_run.sh` is the container half
  and can be run by hand — without `--apply` the bisync stays a dry run;
  `ydm_bisync_job.sh` is the thin Termux half; `install_job.sh` registers it.
  Splitting them is what makes the part with the opinions testable.
- **A failing rename preflight now says so.** It still lets bisync proceed —
  a broken guard must not stop syncing — but it writes
  `rename preflight FAILED` to `var/bisync.log`, because the alternative is
  what actually happened: the guard absent for hours behind a green log.
- **Prune runs daily from the job.** One local scan per run had reached 3865
  scans and 2.14 M rows — 610 MB, 95.3% of it droppable.
- **The filename restriction is measured, not inferred.** On Android 15 a name
  with `|` and `:` is refused by shared storage and the full-width substitutes
  are accepted, while *both* succeed on the container's own filesystem. So the
  `encoding` option is the preventive fix, `repair_android_names.py` keeps the
  corrective one, and `KNOWN_ISSUES.md` now says which situation each answers —
  and that applying the encoding to an existing mirror re-downloads it.
- **The scroll advice was wrong in both halves.** The symptom is touch
  printing junk characters into the command line (mouse reporting left on),
  not swipes acting like arrow keys; and `termux-scroll-fix` is not a Termux
  command, it was a function in one `~/.bashrc`. Both READMEs now carry the
  three lines that actually clear it.
- **261 tests, zero skips, on the phone.** The eight rclone-backed checks that
  CI skips for lack of rclone run here.

## 2026-08-24 — the menu catches up with the tools it sits on

Asking "how do I run the smart cloud scan from the menu" turned out to have no
answer, and that was one symptom of a wider drift. Audited in
[`tasks/ydm_menu/AUDIT-2026-08-24.md`](tasks/ydm_menu/AUDIT-2026-08-24.md),
designed before any screen was touched, then built. 248 tests → 260.

- **Item 7 asks what changed before offering to scan.** One request answers
  whether anything moved at all; if something did, `cloud_delta` names the
  stale folders — about nine requests where a full walk takes ~4 600 — and the
  menu offers to refresh exactly those. "Nothing has changed" is reported as
  the useful answer it is. Without a token the manual chooser still works, and
  a truncated sweep says so rather than passing for a clean one.
- **`d` shows the cloud-vs-local diff**, which is what this project is for and
  had been reachable only from the command line. New entries went on letters
  rather than renumbering: `2` for "add from cloud" is in the owner's fingers
  and written into `HOW_TO_USE.md`.
- **The header says how old the snapshot is** — `base #72, 173 day(s) old,
  serves 48.0% of files`. `sync_tree` had printed this for weeks; the menu
  never did, so anyone working from it decided on a snapshot whose age nobody
  had mentioned. Same rule as the tree: the age is always stated, the warning
  only when the stale part is actually compared.
- **Three screens stopped offering paths from one machine.** They listed
  `/Books/Math`, `/DAO`, `/pro/agents` — meaningless elsewhere, and `/Books`
  is disabled here outright, so the list had gone stale for its author too.
  They now read the snapshot's top level from the database rather than through
  the backend, which keeps opening a menu from firing an `rclone lsf`.
- **Detailed status stopped inventing bisync fields on the daemon.** The
  header already refused to; the same screen was honest above and made-up
  below.
- **The screens have tests at last** — and they came first, because 8.1–8.4
  change four of the six. Nothing had blocked them: `Reader` was always
  injectable. `scripted_reader()` raises when a screen asks for more input
  than the script holds, so a wrong script fails instead of hanging.
- **One of those tests reached the live API** on its first run: in-process, so
  the bench's environment does not apply and `.env` does. The cloud calls are
  stubbed now — a menu test must not be able to touch the network.

## 2026-08-24 — a failing command now exits non-zero

**Behaviour change for every command.** `ydm.py` used to exit 0 whatever
happened — "token not found", "scan failed", "another scan is already
running" all printed an error and returned success. `ydm.py … && next-step`
ran the next step regardless, and the two callers in `tools/sync_backends.py`
that build `"error": None if returncode == 0 else …` could never see a
failure they were written to catch.

- **Three lines, not twenty-one.** The gap analysis expected a change at every
  site that prints an error. It wasn't needed: they all go through `render()`,
  so `render()` now remembers that it printed a failure, `run()` returns the
  "was this command handled" answer it used to discard, and `__main__` turns
  the two into an exit code. No handler was touched — which is also why the
  defect survived so long: their `return True` means "I handled this", and
  reads like "this went well".
- **`run()` returned nothing at all**, so the first cut of the fix failed
  every command including `init`. Caught by the tests, not by reading.
- **One of the new tests passed for the wrong reason** and was rewritten: the
  report type is a positional argument, so `report --type scan-info` made
  argparse reject an unknown flag and exit 2. Non-zero, unrelated, and green
  against the old code too. The docstring now says so.
- Six tests, 240 → 246. Removing the `sys.exit(...)` fails three of them.

## 2026-08-24 — publication decisions, and the January report comes in from the cold

Answers to the four open questions in
[`tasks/opensource/`](tasks/opensource/BACKLOG.md) Phase 4.

- **The incident write-up gets published as it stands.** Honest accounts of an
  AI agent deleting live data are rare enough to be worth more than the
  awkwardness.
- **`tasks/` stays in Russian, and the README says so** rather than leaving a
  reader to work it out: Yandex Disk is a Russian service and so are most of
  the people auditing one. The English side is meant to stand alone, and a
  place where it doesn't is a bug worth reporting.
- **The repository has a description and topics** — `yandex-disk`, `rclone`,
  `sync`, `bisync`, `backup`, `cli`, `python`.
- **January's readiness report is now a tracked document**
  ([`tasks/opensource/2026-01-30-readiness-report.md`](tasks/opensource/2026-01-30-readiness-report.md))
  with a preface saying where it was right and where it was wrong. It had been
  gitignored, so the choice was never "publish it or not" — it was "keep it or
  lose it". Deleting an untracked file is permanent; leaving it alone was
  worse, since it claims "ready, 9/10" and someone would believe that again.
  Keeping it with the correction also repairs a dangling reference: the audit
  cites it, and no reader could find it.

## 2026-08-24 — the history no longer carries someone's file listing

**Every commit hash before this entry has changed.** The repository's history
was rewritten with `git filter-repo` to drop about 84 KB of the owner's
personal file listing — `junk_list.txt`, `junk_list.txt.old`,
`deleted.log.old`, `junk_analysis.json`, `cleanup_paths.txt` — which had been
committed in the initial commit and only ever removed from the working tree.
A January readiness report ticked this off with `git rm --cached`, which
touches the index and not the history.

- **Nothing leaked.** The repository has been private throughout, and all 405
  blobs in the new history scan clean for token patterns, as the old ones did.
- **A third party's name went with it**, replaced by `Sample` in the same pass.
  That decision belonged with the rewrite, not after it: publishing makes
  anything in history public permanently, so cleaning the working tree
  afterwards would have been theatre. The owner's own folder names stay —
  the repository carries their name anyway.
- **Rehearsed before it was run**: a full `git bundle` first, then the whole
  rewrite on a throwaway clone — five files gone, tracked file list unchanged,
  240 tests green, rescan clean, `.git` down from 4.1 MB to 1.8 MB — and only
  then the real thing.
- **The cost was smaller than the risk register claimed.** "Rewriting breaks
  hash references in the docs" turned out to be exactly one document: this
  task's own, written the same day.
- **A rewrite is not enough, and this was checked rather than assumed.** After
  the force-push both servers still hand over the old commit — and the file —
  by direct SHA: `gh api "…/contents/junk_list.txt?ref=<old sha>"` returns its
  24 888 bytes, and GitLab serves the same commit to `git fetch`. Force-pushing
  moves a branch; the objects survive until garbage collection. Harmless while
  the repository is private, and a blocker for publishing, so it is now
  Phase 5 rather than a footnote. There are no forks, so nothing propagated.

## 2026-08-24 — the commands the README opens with now exist

Preparing the repository for publication
([`tasks/opensource/`](tasks/opensource/README.md), Phase 2). The README used
to begin with fifteen shell aliases that did not exist after a clone — they
lived in one person's `~/.bashrc`, and the section documenting them ended with
`source ~/.bashrc` without ever saying what to add.

- **[`tools/aliases.sh`](tools/aliases.sh)** ships the definitions. `YDM_ROOT`
  comes from where the file itself sits, so it works from any checkout.
  Writing it turned up that the README also described `ydm-sync-pick`,
  `ydm-sync-state`, `ydm-help --plain` and `ydm-bisync-resync` — none of which
  exist on the author's machine either. Only what works went in.
- **`/data/ya_disk` is no longer a default** anywhere. `scan local` with no
  path says what to do instead of scanning a directory from someone else's
  machine, and `YDM_LOCAL_ROOT` has no default for the same reason: guessing
  means syncing the wrong folder.
- **Both READMEs open with something that works after `git clone`**, and the
  fact that `ydm-sync-add`/`ydm-sync-rm` apply immediately — rewriting
  `exclude-dirs` and restarting the daemon — is now stated where the commands
  are introduced, with a link to the incident that shows what that looks like
  when it goes wrong.
- **Verified as a stranger would see it**: a clone of tracked files only,
  `env -i` with an empty `HOME`, no configuration — `--help`, the full test
  suite, sourcing the aliases, and the guard firing when `YDM_LOCAL_ROOT` is
  unset.
- **Found and not fixed:** every `ydm.py` command exits 0, including the
  failures. It is one convention across some fifteen call sites, not a typo,
  and changing it changes the contract of every command — recorded as
  [`tasks/opensource/GAP.md`](tasks/opensource/GAP.md) G4a rather than bolted
  onto a documentation pass.

## 2026-08-24 — the rclone paths get exercised, without a device

"On Android `ydm-menu` works through rclone" had been the last open checklist
item since August 16, blocked on "needs a second machine". It was three checks
in one line, and two of them were never about the device. Written up in
[`tasks/android_verify/`](tasks/android_verify/README.md); 232 tests → 240.

- **The bench grew a cloud.** A real directory of files matching the snapshot,
  plus an `rclone.conf` of its own with `cloud:` as an *alias* remote — a bare
  `type = local` would resolve `cloud:pro` against the current directory, so a
  check could have quietly answered a different question.
- **Eight rclone-backed checks now run for real**: the menu over `--backend
  rclone`, `rclone lsf` (the mechanism behind `ydm-sync-pick`), `rclone copy`
  through `sync_filters add --apply`, and `bisync` — `resync --apply`
  establishes the baseline and a plain `run` stays a dry run. Whether bisync
  could be covered at all was an open question in the backlog; it can.
- **This is the first time the bench answers what the system *does* with
  files**, not only what it says. The boundary recorded in
  `tasks/sync_bench/` moved, and that claim was corrected rather than left
  standing.
- **`YDM_VAR_DIR`, because the bench could not isolate what writes logs.** The
  first check that reached a real `rclone copy` wrote into the project's live
  `var/` — `rclone_copy_materialize()` and `rclone_bisync_run()` derive their
  log paths from `PROJECT_ROOT`, and no argument reaches them. A mutation run
  later dropped a `bisync_state.json` there too: a sync baseline, on a machine
  that does not use bisync. One override in `var_path()` redirects all 36 call
  sites, and a guard compares the live logs' mtimes across a bench run.
- **A skip is not a pass.** GitHub Actions has no rclone, so those eight are
  skipped there — verified by running with rclone off `PATH` (`skipped=8`, no
  failures). Keeping them green is the developer machine's job, and that is
  worth remembering when reading "CI is green".
- **Still open, and still needing a device:** whether adding `Colon,Pipe` to
  the local backend's encoding clears `operation not permitted` on `/sdcard`.
  What is now pinned locally is the mechanism — with the encoding set, `|` and
  `:` never reach the disk and `lsf` gives the original name back, where the
  repair script recovers nothing.

## 2026-08-24 — a disabled folder stops counting as a synced one

G6 was recorded yesterday as a wrong marker. It is one conflation with three
consequences, and the marker is the least of them. `policy_paths_set()` put
`bidirectional`, `download_only` and `disabled` into one set, and two of its
three callers used that set to ask *"does anything below this path sync"* — a
question a disabled entry answers backwards. Split into
`synced_policy_paths_set()`, which holds only the entries that sync something.
Details in [`tasks/sync_bench/GAP.md`](tasks/sync_bench/GAP.md#g6).

- **A folder inside a disabled tree no longer claims a synced child.** It used
  to render `[P]`, "parent of a synced path", with nothing synced beneath it.
- **The collapsed tree stops expanding excluded subtrees.** Collapse exists to
  show synced branches; under a disabled entry it was doing the opposite and
  keeping the whole subtree. What stays visible now is only what the separate
  "collapse keeps local orphans" rule keeps — folders that are on disk.
- **The orphan list no longer skips a folder for having a descendant.** Two
  folders in identical standing — on disk, inside a disabled tree, no entry of
  their own — differed only in whether the snapshot held a child of theirs, and
  that alone decided which one the menu offered. This is the worst of the three:
  a missing row is indistinguishable from "not an orphan" by reading the output.
- **`[L]` inside a disabled tree is deliberately kept.** Being on disk and
  outside the policy is a local fact, not a claim about syncing, and wanting one
  folder inside an unsynced tree is a real wish.
- **The bench had no case for two of the three.** A folder on disk inside a
  disabled tree *with a descendant* did not exist in the sample tree, and
  without it neither consequence appears. Added as `/Books/Keep` and
  `/Books/Keep/Old`; 15 paths → 17, 227 tests → 232. Putting `disabled` back
  into the set fails five of them.
- **Measured on the real snapshot before committing.** `var/sync_policy.json`
  holds 52 disabled entries and not one that syncs, so the set of synced paths
  is empty and *every* `[P]` in a whitelist render was false: 687 of 3810 nodes
  change, 665 to `[.]` and 22 to `[L]`. The daily tree is unaffected — it runs
  the daemon backend, which never used this set — but `ydm-menu orphans` always
  renders as a whitelist, and it gains those 22. That configuration now has a
  bench case of its own.

## 2026-08-23 — the tree and the menu get checked against a bench

Implements what the previous entry recorded.
[`tests/bench.py`](tests/bench.py) builds a policy, a local disk and a cloud
snapshot in a temporary directory; [`tests/test_sync_bench.py`](tests/test_sync_bench.py)
asks the real tools what they make of it. 227 tests, up from 194.

- **All nine markers, both semantics.** The sample tree is 15 paths chosen so
  every value of `display_marker()` is reached, rendered under whitelist and
  blacklist policy and compared against a table written before the code ran.
  It also pins something previously unstated: `[D]`, `[D?]`, `[L]`, `[P]` and
  `[.]` cannot arise under blacklist semantics at all, because every path is
  covered there.
- **The truth table went from four values to nine**, plus a test that it has
  not quietly stopped being exhaustive.
- **Two findings on the first run.** Counts are subtree-wide, so a folder is
  `[B]` only when everything beneath it is materialized — the first draft of
  the table put a `[B?]` case inside a `[B]` case and the parent correctly came
  out `[B~]`. And under whitelist semantics a folder inside a *disabled* one
  renders `[P]`, "parent of a synced path", although nothing below it is
  synced: `is_under_policy_path()` counts a disabled entry as a policy path.
  Recorded as G6 and asserted as it behaves, so it cannot change unnoticed.
- **The checks were checked.** Four mutations were applied to the source and
  reverted. One — moving the `[P]` branch above the mode branches — **passed
  everything**, because no case had a folder with both a mode of its own and a
  synced descendant; those cases now exist, and it fails as it should. Another
  — collapsing the dual-convention path lookup — is caught **only** by the
  bench and not by the unit tests, which is the whole argument for the
  end-to-end layer.
- **Both manual checklists are retired**, replaced by a table mapping each item
  to the test that now covers it, and an explicit list of what stays manual:
  bisync and the lock (real data movement), the resync prompt, and Android via
  rclone (still needs a second machine).

## 2026-08-23 — what is left unverified, written down before it rots

No code in this entry. [`tasks/sync_bench/`](tasks/sync_bench/README.md)
records the one place August's work did not reach: the layer the user
actually looks at.

- **The markers are covered less than halfway.** `display_marker()` returns
  nine values; four are tested (`[B]`, `[X]`, `[L]`, `[D?]`). The five that
  are not include `[.]`, the default every unmatched case falls into — a
  wrong branch order above it would return `[.]` and no test would notice.
  (Correcting an earlier note in `tasks/sync_tree/BACKLOG.md`: `[B]` and `[L]`
  are the covered ones, not the gaps.)
- **There is no end-to-end render test at all.** The four covered values are
  checked by calling a pure function with arguments assembled by hand in the
  test body. That the right arguments reach it is verified by nothing — which
  is the exact shape of blindness that let the `parent_path` mismatch live
  until 16.08. The only test that runs `sync_tree.py` as a program checks
  `--help`.
- **The manual checklists cannot be run as written**, and not merely because
  they name a policy that no longer exists. Recreating `Books/Math/База` as
  bidirectional on the live daemon means dropping `Books` from `exclude-dirs`,
  adding sibling exclusions at every level (55 entries → 97), and starting a
  real sync of `/Books` — the operation behind the 14.08 incident. Both
  checklists are now marked accordingly instead of sitting there looking
  actionable.
- The design settles the one open question: a synthetic bench, not the live
  configuration. `ydm_menu` already accepts `--db-path`, `--local-root`,
  `--policy-path`, `--backend` and `--non-interactive`, so it addresses a
  bench without a single change — which is what makes this cheap.

Nothing here is a reported failure. `[B~]`, `[P]` and `[.]` render plausibly
today. It is written down because "not observed to be broken" and "works" are
different claims, and the four defects fixed this month lived in the gap
between them.

## 2026-08-23 — the coverage gate loses its back door

The rule the whole month was built on — a partial scan must never be the
composite's base — had a path around it. When `find_last_full_scan()` returned
nothing, `build_composite_scan()` took the most recent cloud scan instead,
with no check of coverage or status. That turned the gate off at the exact
moment it had done its job: it returns nothing only when every candidate was
rejected, and the fallback then installed a rejected one anyway.

Reproduced on a synthetic database holding only partial scans: the composite
named a depth-limited one-file scan of `/` as the base for the whole disk,
with no error and no warning.

- The distinction the old path missed: a fallback may relax the *soft*
  criterion, freshness, and never the *hard* one, coverage.
  `find_last_full_scan()` already relaxes freshness internally by falling back
  to the newest root-covering scan however old it is — so its `None` is the
  final answer, not an invitation to try something else. The composite now
  returns an error naming the remedy (`scan cloud` without `--path`/`--depth`).
- Callers already survive that error: `get_diff()` drops to a single-scan
  comparison that says so in `compare_scans`, `cloud_delta` raises with the
  text. `sync_common.build_composite_snapshot()` used to replace the reason
  with "missing base_scan_id" and now passes it through.
- Recorded as [`tasks/diff_correctness/GAP.md`](tasks/diff_correctness/GAP.md)
  and Phase 4 of that backlog. It had been sitting as `sync_tree` 3.4
  "Analyzer fallback improvement — deferred" since 2026-08-08, which is how it
  survived the month of work on exactly this defect class.

Nothing changed for the reference database: base #72, 2251 folder updates.
The gap was a mine, not a fire — it would have gone off after an aggressive
`report prune`, on a new machine, or anywhere the full scan was lost.

## 2026-08-23 — the staleness warning stops crying wolf

The freshness warning added earlier the same day was correct and useless. It
reported that 48% of the snapshot came from a 172-day-old scan and that
findings there might be stale — but all 36 028 of those files sit under
`exclude-dirs` (`downloads`, `журналы`, `music` and eighteen others), which
nothing ever compares against the local copy. There are no findings there to
be stale about. The warning would have fired on every run for the rest of the
project's life while changing no decision, and a warning that always fires is
one nobody reads when it finally means something.

- `snapshot_freshness()` now splits base-served files into
  `stale_compared_files` and `stale_excluded_files`, and warns only about the
  first. The age and share are still reported either way — the fact was never
  the problem, the alarm was. `sync_tree`'s header says which kind it is
  (`… serves 48.0% of the files (none of them synced)`).
- `report diff` passes its own exclusion list in, so "never compared" means
  exactly what that diff means by not comparing it, rather than two readers
  deriving it separately and drifting.
- `load_exclude_dirs()` is now one function instead of an inline block inside
  `get_diff()`.

## 2026-08-23 — the diff reconciles exactly, for the first time

Both sides of `report diff` now add up with nothing left over:
`74 984 cloud = 14 104 matched + 60 880 excluded + 0 missing` and
`14 138 local = 14 104 matched + 34 ignored + 0 missing`.

- **Scans record their own scope.** `scans.scan_root` and `scans.scan_depth`
  store what a scan was *asked* to cover. Scope used to be reconstructed from
  the rows a scan left behind, and that inference was wrong twice — once
  naming a deep leaf as a partial scan's root, once about to let a shallow
  scan of `/` pass for a full one. Both columns are nullable; older scans
  carry NULL, keep the inference path, and are granted none of the new
  powers below.
- **`scan cloud --path / --depth N`** bounds the walk. The disk root was the
  one folder with no cheap refresh — a full walk of `/` is 1.5 TB, while the
  files sitting directly in it are eight requests. Measured: **0.9 seconds**.
  A depth-limited scan is a partial update by construction and can never
  become the composite base.
- **A rescan can now report a folder as gone.** The composite served any
  folder no partial scan touched from the base, which is right for a folder
  nobody looked at and wrong for one that was looked at and no longer exists.
  That is why `report diff` still claimed a file under `/brtn/Запчасти/фото`,
  a folder deleted long enough ago to be out of the trash — invisible to any
  delta sweep, and unfixable by rescanning, because "absent" and "uncovered"
  were the same thing. A scan may retire a folder only if it is `success`,
  recorded a non-root `scan_root`, and was not depth-limited.
- **`local_ignored_count`** declares the files under `.sync` that the local
  side used to drop silently. The cloud side has always declared its
  exclusions; an undeclared hole on the other side is how the last three
  defects stayed invisible for months. A companion invariant test asserts the
  local counts balance, alongside the cloud one that already existed.
- Three dead entries (`Books (1)`, `Books_LOCAL_142_20260815`,
  `Books_TEST_RESTORE_FILE_20260815`) removed from `var/sync_policy.json` and
  the daemon's `exclude-dirs`, 55 → 52, after confirming via the API that all
  three are absent from the cloud and from `/data/ya_disk`. `Books` itself
  stays excluded.

## 2026-08-23 — the snapshot now says how stale it is

- **`snapshot_freshness()`** reports the composite base's age, how many files
  still come from it, and what share of the snapshot that is. It warns when an
  old base still serves a large part of the tree, and points at
  `tools/cloud_delta.py changes` for what to rescan. On the reference
  database: 48.1% of the snapshot (36 029 files) came from scan #72, 165 days
  old — which is why the previous day's "1 missing file" was a March artifact
  presented as a live finding.
- Surfaced where the numbers are read: `report diff` gains
  `snapshot_freshness` and `warnings`; `sync_tree` prints a `snapshot_age:`
  line plus `WARN:` lines, and carries
  `header.cloud_snapshot.snapshot_freshness` in JSON.
- **Delta scan step 3 is decided against**, with the reasoning recorded in
  [`tasks/delta_scan/README.md`](tasks/delta_scan/README.md). A per-file
  overlay would be a second, weaker writer for the snapshot — it cannot
  express directory rows, empty folders, or absence — and the only thing it
  saves is the 4 minutes that rescanning the 31 flagged folders actually took.
  Steps 0–2 stay a pointer at what to rescan; `scan cloud --path …` stays the
  single writer.

## 2026-08-16 — `report prune`, and a guard against the previous entry

- **`report prune`** deletes scans the composite no longer needs. Dry-run by
  default; `--apply` deletes, `--vacuum` shrinks the file. It never touches
  the composite base, any scan the composite draws a folder from, an explicit
  `reference_full_scan_id`, the `--keep-root-scans` most recent full scans
  (history is a purpose of this project, not overhead), anything newer than
  the base, or the `--keep-local` most recent local scans — and it prints
  what it kept and why. On the reference database: 46 scans kept, 91
  prunable, 1 192 805 of 1 439 569 rows (82.9%).
- **`TestDiffInvariant`**: a cloud scan and a local scan describing the same
  tree must diff to nothing — with each side written in its own native
  convention. This is the test that would have caught the path-convention
  defect on day one; every earlier diff test built both sides from the same
  string, so they agreed by accident. A companion test asserts the invariant
  still detects a single planted difference, because a guard that always
  reports zero is worse than none.
- The `parent_path` convention is now documented in
  [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), including why storage keeps
  two conventions rather than being migrated to one.

## 2026-08-16 — `report diff` never matched anything

The project's headline feature — compare cloud against the local copy — was
comparing two sets that cannot intersect. Cloud scans store `parent_path` as
`/pro/MuSy`, local scans as `pro/MuSy`, and the comparison joined them raw:

```text
exact join, cloud scan 72 x local scan 105:   0 matches
after stripping the leading slash:         9428 matches
```

So every file was reported both as missing locally and as missing in the
cloud. `missing_cloud_count` equalled the local file count exactly. The
convention is uniform across the database (0 of 856 338 local rows carry a
leading slash), so this was the original behavior, not a regression — the
existing tests used `/A` on both sides and never exercised it.

Four defects, one function:

- **Path conventions.** `normalize_compare_path()` settles it in one place.
  Storage is left alone: rewriting 856 k historical local rows would mean
  touching every reader of the local side too.
- **Nested `exclude-dirs` entries were ignored.** The filter tested only the
  first path component, but 45 of the 55 entries here are nested
  (`video/Обучение`). `is_path_excluded()` walks the whole path.
- **Composite assembly was a cross product.**
  `WHERE scan_id IN (…) AND parent_path IN (…)` paired every scan with every
  folder, so any scan holding rows for a folder could win at random instead of
  the one the composite assigned.
- **Three copies of the comparison**, one of them unreachable behind earlier
  `return`s. That is how a defect this size survived: a fix in one copy never
  reached the others. Now one implementation, 18 576 → 5 595 characters.

On the real database: 14 101 of 14 136 local files matched, 60 880 correctly
identified as excluded from sync, **1** missing locally and 3 missing in the
cloud — and every one of those four is explainable. Analysis and remaining
work: [`tasks/diff_correctness/`](tasks/diff_correctness/README.md).

## 2026-08-16 — Composite file counts: wrong, and then slow

Surfaced by the previous entry's fix: with the snapshot finally carrying its
real 2 249 folder updates, `sync_tree --path /` took **176 seconds**.

`count_cloud_files_for_path()` added the base scan's recursive total for a
subtree, subtracted a recursive total per updated folder, then added each
updated folder's recursive total back. Three problems:

- **Nested updates were subtracted twice.** `/A` and `/A/B` both updated
  meant `/A/B`'s files came off the base count once for each.
- **Siblings were matched by raw string prefix**, so counting `/pro` also
  pulled in `/protein`.
- It issued several `COUNT(*)` queries per updated folder — ~13 000 queries
  over 1.4 M rows for the root.

It now sums per folder, the way the composite actually resolves, with one
`GROUP BY` per scan involved: **176 s → 0.07 s** for the root, and `/Books`
counts 13 893 files, matching the verified restore.

The same bug was inflating the tree's percentages — `brtn` used to render at
208% synced.

## 2026-08-16 — The composite kept 6% of every partial scan

Found by running the new `cloud_delta.py` against the freshly rescanned
folders: they were still reported as stale. `build_composite_scan()` filters
each partial scan's folders by "must be under the scan's root", and derived
that root as *the `scan_progress` row with the earliest `last_checked`*. But
`last_checked` marks when a folder **finished**, and the first folder to
finish is a deep leaf, not the root.

Scan 93 (`scan cloud --path /Books`, 437 folders) resolved to
`/Books/ментальные карты/yang_super`, so 436 of its 437 folder updates were
discarded. `/pro` and `/Компьютер WIN-…` fared the same. Across the database
the composite carried **136 folder updates where it should have carried
2 249** — partial scans have been mostly decorative, which is exactly the
"unreliable mechanism" this design was suspected of being.

- `Analyzer.scan_root_path()` derives the root as the common ancestor of
  every folder the scan recorded (`scan_progress`, falling back to
  `files.parent_path`). Retroactive: no rescan needed for existing data.
- The old fallback ("shortest parent_path", implemented as
  `ORDER BY parent_path LIMIT 1`, i.e. alphabetically first) is gone with it.

After this plus the 31 targeted rescans that `cloud_delta.py` planned, the
snapshot went from 67 stale folders to 1 — the disk root itself, whose direct
files only a full scan refreshes.

## 2026-08-16 — `cloud_delta.py`: which folders of the snapshot went stale

The composite snapshot patches a base scan with targeted partial scans, so it
is only as fresh as the folders someone thought to rescan — and there was no
way to find out which ones needed it. `tools/cloud_delta.py` answers that
without walking the tree, using three endpoints the project had not been
using:

- `GET /v1/disk` returns a global revision counter. Unchanged since the last
  check → nothing changed anywhere on the disk, and the whole run is one HTTP
  request. (It is the same counter the daemon tracks: the value matched, to
  the digit, the `"new"` revision in `.sync/push.log` for the last change.)
- `GET /v1/disk/resources/files?sort=-modified` lists the most recently
  modified files across the whole disk, flat and newest first. Paging it until
  the timestamps predate the snapshot costs `ceil(changed/1000)` requests,
  independent of disk size.
- `GET /v1/disk/trash/resources?sort=-deleted` supplies deletions, each with
  the `origin_path` it came from.

Staleness is decided per folder, not per disk: the composite covers each
folder with the newest scan that visited it, so the same timestamp is news in
one folder and old news in another. Output is a rescan plan for the existing
scanner, with nested folders collapsed to the fewest `scan cloud --path …`
commands that cover them.

First run here: **9 requests against ~4 600 for a full scan** — 67 stale
folders, 662 changed files, 6 deletions, 32 rescan commands. Read-only;
`monitor.db` is not touched. Blind spots (moves, renames, restores from trash
keep their `modified`) are documented and warned about rather than passed off
as "no changes":
[`tasks/delta_scan/README.md`](tasks/delta_scan/README.md).

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
