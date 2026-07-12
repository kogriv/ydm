# Known Issues

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
