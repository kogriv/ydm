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

Measured on the device, Android 15 (SDK 35), 2026-08-24 — writing a file whose
name holds `|` and `:`:

| Target | Raw `\|` and `:` | Full-width `｜` and `：` |
|---|---|---|
| `/sdcard` | **refused** (`Operation not permitted`) | ok |
| the proot container's own filesystem | ok | ok |

The second row is the one that settles it: the restriction belongs to shared
storage, not to proot, Python or the kernel.

**Impact:** the main `rclone copy` can finish most of a folder and still
return non-zero for only the incompatible names. In one observed
`/pro/agents` sync, 1.926 GiB transferred successfully and 13 small PDF/IPYNB
files failed.

**Preventive fix, for a mirror you are about to create:** rclone's local
backend has an `encoding` option that substitutes the forbidden characters
before they reach the disk, and translates them back on read — so the copy
never fails and `rclone lsf` still reports the original name:

```ini
[android]
type = local
encoding = Slash,Dot,Colon,Pipe
```

Demonstrated as a mechanism in `tests/test_sync_bench.py`
(`test_a_restricted_name_survives_an_encoded_remote`), and the device
measurement above confirms the premise it rests on. What has *not* been run
end to end is a full `rclone copy` of an affected folder on the device itself.

**Do not apply it to a mirror that already exists.** Changing the encoding of
materialized files makes rclone see the existing names as different names, and
it will re-download large objects. That is why the repair below stays.

**Workaround, for a mirror that already has the damage:** repair only the
incompatible names after the normal sync. This is the supported use of
`tools/repair_android_names.py` — it is not deprecated by the encoding option,
because the two answer different situations (prevention vs. a mirror whose
original names were already lost):

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

**Related:** this is also why the policy layer has a `download_only` mode — a
path can be worth mirroring locally and unsafe to sync back, because the local
names are not the cloud names. The full Android arrangement is in
[`ANDROID_SETUP.md`](ANDROID_SETUP.md).

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
