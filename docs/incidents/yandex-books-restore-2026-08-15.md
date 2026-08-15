# Yandex Disk `/Books` Restore Incident

Date: 2026-08-15

## Summary

An external agent running on another machine created an empty `Books` folder via
the Yandex Disk API after the original cloud `disk:/Books` tree had been moved
to Trash. The web Trash UI did not expose a useful one-folder restore path, and
Yandex support initially said they could only restore all Trash contents.

This repo now contains a diagnostic/recovery helper:

```bash
python3 tools/trash_scan.py --db-path var/trash_books.db scan --progress
python3 tools/trash_scan.py --db-path var/trash_books.db summary
python3 tools/trash_scan.py --db-path var/trash_books.db restore-plan
python3 tools/trash_scan.py --db-path var/trash_books.db restore-root --poll --apply --yes RESTORE_ROOT
```

The local scan database `var/trash_books.db` is intentionally not committed.

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

Comparison with historical `/Books`:

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
but the operations later reported `failed`.

After verifying that active cloud `/Books` did not exist, the root Trash resource
restore was retried:

```bash
python3 tools/trash_scan.py \
  --db-path var/trash_books.db \
  restore-root \
  --trash-root 'trash:/Books_25639b9fb1cee52a5b58811baffa033cbf2896a3' \
  --restore-root /Books \
  --poll \
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

Temporary cloud folders existed and were intentionally left untouched:

```text
Books (1)/
Books_LOCAL_142_20260815/
Books_TEST_RESTORE_FILE_20260815/
```

Do not re-enable bidirectional sync until `/Books` is verified from the target
machine and the temporary folders are reviewed.
