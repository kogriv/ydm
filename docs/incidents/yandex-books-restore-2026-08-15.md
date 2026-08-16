# Yandex Disk `/Books` Restore Incident

Date: 2026-08-15

## Summary

The cloud `disk:/Books` tree (13 893 files, 113,6 GB) was moved to Trash, and
an empty `Books` folder was created over it via the API. The web Trash UI did
not expose a useful one-folder restore path, and Yandex support initially said
they could only restore all Trash contents.

**Attribution correction (2026-08-16).** This document originally blamed "an
external agent running on another machine". It was not external: the deletion
was caused by an AI-agent session in *this* repository, via a bug in
`_policy_coerce_for_daemon()` that removed `Books` from the daemon's
`exclude-dirs` shortly before its local copy was deleted. Root cause, evidence
and timeline: [`yandex-books-delete-2026-08-14.md`](./yandex-books-delete-2026-08-14.md).

This repo now contains a diagnostic/recovery helper:

```bash
DB=var/trash_scan.db
TRASH_ROOT='trash:/<name reported by the trash listing>'

python3 tools/trash_scan.py --db-path $DB scan \
  --trash-root "$TRASH_ROOT" --restore-root /Books --progress
python3 tools/trash_scan.py --db-path $DB summary --restore-root /Books
python3 tools/trash_scan.py --db-path $DB restore-plan --restore-root /Books
python3 tools/trash_scan.py --db-path $DB restore-root \
  --trash-root "$TRASH_ROOT" --restore-root /Books --apply --yes RESTORE_ROOT
```

`--trash-root` / `--restore-root` are required: a mutating command must not
carry one incident's paths as defaults. The token comes from `.env`
(`YANDEX_DISK_TOKEN`) by default; `--token-source rclone` reads the rclone
remote instead.

The local scan database `var/trash_scan.db` is intentionally not committed.

## Recovered Resource

Trash root found by API:

```text
trash:/Books_25639b9fb1cee52a5b58811baffa033cbf2896a3
```

Original origin reported by Yandex:

```text
disk:/Books
```

Deletion time reported by Yandex:

```text
2026-08-14T09:42:44+00:00
```

## Scan Results

Read-only Trash scan completed successfully:

```text
rows: 14721
files: 13893
dirs: 828
bytes: 113648649100
errors: 0
```

Historical `/Books` from `monitor.db`, scan `1`:

```text
rows: 14551
files: 13728
dirs: 823
bytes: 112378920102
```

Comparison with historical `/Books` (`compare-monitor` reports all five of
these; the size/md5 metrics came from ad-hoc SQL when this was written and are
part of the command since 2026-08-16):

```text
matched_files: 13726
missing_old_files: 2
extra_trash_files: 167
size_mismatch_matched_files: 0
md5_mismatch_matched_files: 0
```

The two missing historical files were:

```text
/Books/Math/База/Имяславие(1).djvu
/Books/Math/База/Имяславие(2).djvu
```

The fresh partial `/Books/Math` monitor scan `2426` matched the Trash scan
exactly:

```text
files: 2133
dirs: 191
bytes: 14459060747
missing: 0
extra: 0
size mismatches: 0
md5 mismatches: 0
```

## Restore Attempts

The whole-folder restore initially returned:

```text
423 DiskResourceLockedError
```

Per-file restore of nested Trash entries returned `404 DiskNotFoundError` for
some files. Restoring top-level child directories accepted operations (`202`)
but the operations later reported `failed`. That is exactly why `restore-files`
now polls `/operations/<id>` and records `accepted`/`failed` instead of taking
a `202` for a completed restore (`poll-ops` resolves leftovers later).

After verifying that active cloud `/Books` did not exist, the root Trash resource
restore was retried:

```bash
python3 tools/trash_scan.py \
  --db-path var/trash_scan.db \
  restore-root \
  --trash-root 'trash:/Books_25639b9fb1cee52a5b58811baffa033cbf2896a3' \
  --restore-root /Books \
  --apply \
  --yes RESTORE_ROOT
```

```text
restore HTTP status: 202
operation href: https://cloud-api.yandex.net/v1/disk/operations/4d40dfa783d91dabb0037ad328f2f651a575026a4e7b55b51b0877f7e56e77e5
operation status: success
```

Post-restore verification:

```bash
rclone size yandex:/Books --json
```

Result:

```json
{"count":13893,"bytes":113648649100,"sizeless":0}
```

After successful restore, the Trash root returned `404`, which is expected
because it had been removed from Trash.

## Current Cautions

At the time of restore, scheduled Termux bisync jobs were disabled:

```text
termux-job-scheduler --pending
No pending jobs
```

Temporary cloud folders existed and were left untouched at the time:

```text
Books (1)/                          1 file   (an agent's test.txt)
Books_LOCAL_142_20260815/         142 files  (894 MB safety copy)
Books_TEST_RESTORE_FILE_20260815/   1 file   (single-file restore test)
```

**Removed 2026-08-16**, after verifying against the post-restore full scan
(`93`) of `/Books` that every one of the 142 files exists there with identical
size and md5 — 0 missing, 0 mismatches — and that the test-restore file's
content (`1254fc02f859bd67cbc4947c5b94b37d`) is present in `/Books` in four
places. They went to Trash, not permanent deletion. Their names stay in
`exclude-dirs` deliberately: removing them would need a daemon restart, which
is not worth doing for stale entries pointing at paths that no longer exist.

`/Books` was verified from the target machine on 2026-08-15 (scan `93` vs the
last full pre-incident scan `72`: 0 files lost). Do not re-enable
bidirectional sync for paths whose local copy is incomplete — see
[`yandex-books-delete-2026-08-14.md`](./yandex-books-delete-2026-08-14.md) and
the `deletion_risk_paths` guard it produced.
