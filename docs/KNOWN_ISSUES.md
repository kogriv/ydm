# Known Issues

## Android shared storage rejects some cloud filenames

**Symptom:**
```
Failed to copy: open /sdcard/Download/ya_disk/.../Agents Week 2026 | ...pdf: operation not permitted
```

**Cause:** Android shared storage (`/sdcard`, `/storage/emulated/0`) does
not allow ordinary apps to create files whose path contains some ASCII
characters that are valid on Yandex Disk, notably `|` and `:`. Rclone's
Yandex backend can list/download those objects, but the local Android
filesystem refuses the final open/create.

**Impact:** the main `rclone copy` can finish most of a folder and still
return non-zero for only the incompatible names. In one observed
`/pro/agents` sync, 1.926 GiB transferred successfully and 13 small PDF/IPYNB
files failed.

**Workaround:** repair only the incompatible names after the normal sync:

```bash
python3 tools/repair_android_names.py \
  --db-path monitor.db \
  --local-root /sdcard/Download/ya_disk \
  --path /pro/agents

python3 tools/repair_android_names.py \
  --db-path monitor.db \
  --local-root /sdcard/Download/ya_disk \
  --path /pro/agents \
  --apply
```

The repair command maps forbidden ASCII characters to full-width lookalikes
locally, for example `|` -> `｜` and `:` -> `：`, and copies files one by one
with `rclone copyto`. It intentionally avoids a broad encoded `rclone copy`,
because changing local encoding for an already-materialized mirror can make
rclone treat some existing files as different names and re-download large
objects.

**Logs:** `tools/sync_filters.py add --apply` now writes the full most recent
`rclone copy` output to `var/copy_last.log` and appends a compact history line
to `var/copy.log`.

## SIGTERM during scan initialization can hang the process

**Symptom:**
```
Lock acquired (PID: 568177)
Starting new cloud scan 42...
Will scan from path: /Books

Received SIGTERM. Graceful shutdown requested...
<process hangs, never exits>
```

**Cause:** graceful shutdown works by setting a flag that the main scan
loop checks between iterations (`ydm.py`, `is_termination_requested()`).
If SIGTERM arrives *before* the loop starts — while `CloudScanner` is
still being constructed / making its first API calls (`ydm.py`, around
where `scanner = CloudScanner(...)` is called in the `scan cloud` command
handler) — the flag is never checked, and the process hangs instead of
exiting.

**Impact:** only affects scans interrupted in their first couple of
minutes (during setup, before any folders are being processed). The
per-run lock file already prevents this from corrupting a concurrent
scan — it's just an inconvenience for whoever sent the signal.

**Workaround:** if a scan doesn't exit within a few seconds of SIGTERM
during this early phase, send SIGKILL (`kill -9`) instead.

**Status:** not fixed. A real fix would mean checking the termination
flag inside `CloudScanner`'s own setup path (or wrapping it with a
timeout), not just in the main loop — contributions welcome, see
[CONTRIBUTING.md](../CONTRIBUTING.md).
