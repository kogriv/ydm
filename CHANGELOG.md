# Changelog

Notable changes to this project, newest first. This project doesn't tag
releases, so entries are grouped by date. Detailed design/acceptance logs
for larger workstreams live in their own docs (linked below) — this file
is a scannable index, not a copy of them.

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
