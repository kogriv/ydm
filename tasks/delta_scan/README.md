# Delta scan — cheap change detection instead of a full rescan

**Status: step 1 built (`tools/cloud_delta.py`), steps 2–3 proposed.** All
numbers below were measured against the live account on 2026-08-16.

## Problem

A full cloud scan walks every folder — ~4 600 requests, ~75 000 rows added to
`monitor.db` — and the vast majority of that never changed. That cost is why
the composite snapshot exists: one full scan as a base, plus targeted partial
scans as patches ([`tasks/smart_diff/`](../smart_diff/README.md)).

The composite trades cost for certainty. A partial scan only refreshes the
folder you name, so the snapshot is accurate exactly where you thought to
look. **We never learn what actually changed in the cloud** — a file added
under a folder nobody rescanned stays invisible until the next full scan.

What's wanted: a mechanism that finds *what changed* without walking the tree.

## What the API actually offers

Measured on the live account, 2026-08-16.

| Call | Cost | What it gives |
|---|---|---|
| `GET /v1/disk?fields=revision` | 1 request, instant | Global change counter for the whole disk |
| `GET /v1/disk/resources/files?sort=-modified&limit=1000` | 1 request, ~4 s | The 1 000 most recently modified **files** on the whole disk, with full paths — flat, no tree walk |
| `GET /v1/disk/trash/resources?sort=-deleted` | 1 request | Deletions, newest first, each with `origin_path` and `deleted` |
| `GET /v1/disk/resources?path=X&fields=revision` | 1 request per folder | That resource's own revision |

Two findings that shape the design:

1. **The global `revision` is the daemon's own change counter.** The value
   returned by `/v1/disk` (`1786876883888838`) matched, to the digit, the
   `"new"` revision in the daemon's `.sync/push.log` diff record for the last
   change made. One request answers "did anything change at all".
2. **Folder revisions do not aggregate.** `disk:/Books/Math` sits at a 2023
   revision while its child `disk:/Books/Math/АнГем` is at 2026-07. So a
   top-down walk cannot prune unchanged subtrees by comparing one revision —
   that shortcut is not available.

## Step 1 — built: `tools/cloud_delta.py`

Read-only. Nothing is written to `monitor.db`; the output is a targeting list
for the existing scanner.

```bash
# One request: has anything changed on the disk at all?
python3 tools/cloud_delta.py check --save

# Which folders of the snapshot went stale, and what to rescan
python3 tools/cloud_delta.py changes
```

First real run on this account:

```text
snapshot: base scan #72 at 2026-03-04, 149 folder update(s)
swept: 5002 file(s), 6 trash entr(ies), 9 request(s)
stale folders: 67  changed files: 662  deleted: 6  rescan roots: 32
```

**9 requests against ~4 600 for a full scan**, and the answer is specific:
`/pro/salva` has 140 files newer than the scan that covers it, `/obsidian_vault`
has 70, and so on — with a `scan cloud --path …` line for each.

Two details that make the output usable rather than merely correct:

- **Staleness is per folder, not per disk.** The composite covers every folder
  with the newest scan that visited it, so the same timestamp can be news in
  `/video` (covered by the March base) and old news in `/pro` (rescanned in
  August). `Snapshot.covering_scan()` answers that per path.
- **Nested folders collapse.** `scan cloud --path X` walks X recursively, so
  67 stale folders became 32 commands. The disk root is deliberately excluded
  from that collapsing: rescanning `/` *is* the full scan.

Limits are reported, never silent: hitting `--max-pages` before reaching the
snapshot date prints a warning saying the list is incomplete, and a disk
revision that moved while the sweeps found nothing prints the blind-spot
warning below instead of "no changes".

### What the first use of it turned up

Running the 32 planned rescans and re-checking showed the same folders still
stale — which exposed a bug in the composite itself, not in the sweep:
`build_composite_scan()` was keeping roughly 6% of every partial scan (136
folder updates where 2 249 were due), because it inferred each scan's root
from the *first folder to finish*, a deep leaf. Fixed in the same session;
see the CHANGELOG entry "The composite kept 6% of every partial scan".

Afterwards: **67 stale folders → 1**, the sole remainder being files sitting
directly in the disk root, which only a full scan refreshes.

That is the argument for this tool in one paragraph. The composite had been
quietly wrong for months and nothing surfaced it, because nothing ever asked
"is the snapshot actually current?" — a targeted rescan looks identical
whether it landed or not.

## Proposed mechanism

Store `disk_revision` alongside each cloud scan. Then:

Steps 0–2 are what `cloud_delta.py` does today:

**Step 0 — is anything different?** One request for `/v1/disk` revision.
Unchanged since the last check → nothing to do anywhere. On a quiet day the
whole thing costs one HTTP call.

**Step 1 — additions and modifications.** Page `/files?sort=-modified` until
`modified` drops below the oldest date the snapshot covers. Cost is
`ceil(changed_files / 1000)` requests, independent of disk size. Each item
carries `path`, `size`, `md5`, `modified`.

**Step 2 — deletions.** Page `/trash/resources?sort=-deleted` the same way.
`origin_path` says what vanished and from where.

**Step 3 — decided against (2026-08-23).** The idea was to persist the delta
as a scan whose rows are the changed paths and teach `build_composite_scan()`
to overlay per *file* rather than per folder. We are not building it:

- A per-file overlay is a second, weaker way to write the snapshot, layered on
  a mechanism that produced three serious defects in one week (a partial scan
  becoming the base, the composite discarding 94% of every partial scan, and
  a diff that never matched a single file). Adding a second write path there
  buys speed at the cost of the property that was hardest to get back.
- A flat `/files` listing cannot express what the composite needs: directory
  rows, empty folders, and *absence* — that a file which used to be in a
  folder is gone. A per-folder replacement carries all three for free; a
  per-file overlay would have to reconstruct them, and would be silently wrong
  when it guessed.
- The thing it would save is not expensive. Rescanning the 31 folders that
  steps 0–2 flagged took **4 minutes and 0 failures**. That is the whole
  saving on the table, against a permanent correctness risk.

So steps 0–2 stay a *pointer*: they say which folders went stale, and the
existing `scan cloud --path …` refreshes exactly those. That keeps one writer
for the snapshot and one meaning for a folder update.

**Built instead: the snapshot says how stale it is.** `Analyzer.
snapshot_freshness()` reports the base scan's age and what share of the
snapshot still comes from it, and warns when an old base still serves a large
part of the tree. It is surfaced in `report diff` (`snapshot_freshness` and
`warnings`) and in `sync_tree` (a `snapshot_age:` line and `WARN:` lines in
text, `header.cloud_snapshot.snapshot_freshness` in JSON). This is the gap
that actually bit: on 2026-08-16 `report diff` presented a March-era artifact
as a live finding, because nothing in the snapshot said 48% of it was 165 days
old.

## Known blind spots

Be explicit about these — a mechanism that silently misses changes is worse
than an honest full scan.

- **Moves and renames.** A move does not go to the trash and, from what the
  restore showed, does not necessarily bump the file's `modified`. It would be
  missed by steps 1 and 2. The *folder's* revision does change, but detecting
  that needs a directory walk, which is the cost we are avoiding.
- **Restores from trash.** Verified on 2026-08-15: the restored `/Books` files
  kept their original `modified` **and** their original `revision`; only the
  folder's revision moved. Invisible to a `-modified` sweep.
- **Changes older than the retained trash.** The trash is finite; a deletion
  purged from it leaves no record. *Partly answered on 2026-08-23:* a
  successful, non-depth-limited rescan of a subtree now retires the base's
  rows for folders inside it that the rescan did not find, so a deletion the
  delta can no longer see is still corrected the next time anyone rescans that
  subtree. It does not tell you *which* subtree to rescan — that part remains
  a blind spot.

Mitigation: keep the full scan as a periodic floor (quarterly, say), with
deltas in between, and record in the snapshot which mechanism produced each
row so staleness is visible rather than assumed. The global revision gives a
useful consistency check: if the disk revision moved but the delta sweep found
nothing, something happened that this mechanism cannot see — worth surfacing
as a warning rather than silence.

## Why this is worth doing

The current alternative to a full scan is "rescan the folders you suspect",
which is guesswork. Steps 0–2 cost a handful of requests and answer the
question directly. Even if steps 1–2 only ever feed a *warning* ("these 40
paths changed since your snapshot — rescan them?"), that converts the
composite from a hopeful approximation into something that knows where it is
stale.

## Related

- [`tasks/smart_diff/`](../smart_diff/README.md) — the composite snapshot this
  would feed.
- [`docs/incidents/yandex-books-delete-2026-08-14.md`](../../docs/incidents/yandex-books-delete-2026-08-14.md)
  — why knowing the real cloud state matters.
- `tools/trash_scan.py` — already speaks the trash API used in step 2.
