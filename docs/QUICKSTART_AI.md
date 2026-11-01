# Quickstart for AI assistants and automation

Rules and step-by-step procedures are in [`AGENTS.md`](../AGENTS.md): read that
first. This page is the reference behind it: the tools, what they print, and how
to handle a long scan.

## The tools

| Tool | What it does | `--format` default | Where `--format` goes |
|------|--------------|--------------------|-----------------------|
| `ydm.py` | Database, cloud and local scans, reports | `text` | before the subcommand |
| `tools/sync_policy.py` | The sync policy: `add`, `remove`, `quarantine`, `ignore`, `render-filters`, `download-plan`, `download-apply`, `import-file` | `text` | after the subcommand |
| `tools/sync_bisync.py` | rclone bisync: `doctor`, `status`, `pause`, `resume`, `resync`, `run` | `json` | after the subcommand |
| `tools/sync_rename.py` | Renames that do not re-upload: `plan`, `apply`, `preflight` | `text` | after the subcommand |
| `tools/sync_tree.py` | The synced tree and its state, from the snapshot | `json` | anywhere |
| `tools/cloud_delta.py` | Read-only: which folders went stale, in a few requests (needs the API token) | `text` | before the subcommand |
| `tools/trash_scan.py` | Scan and restore a Trash subtree | `json` | before the subcommand |
| `tools/sync_filters.py` | Legacy: edits the filter file directly, without a policy | `json` | after the subcommand |
| `tools/sync_exclude.py` | Legacy: edits the daemon's `exclude-dirs` directly | `json` | after the subcommand |

The defaults differ, so always pass `--format json`. Prefer `sync_policy.py` to
the two legacy editors on any machine that has a policy.

State lives in `monitor.db` (scans) and `var/` (`sync_policy.json`,
`bisync_state.json`, `bisync_pause.json`, `bisync.log`, `bisync_last.log`,
`rclone_binary`).

## Output contract

```json
{"success": true, "data": {"schema": "ydm_sync_policy:v2", "action": "add", "dry_run": true, "error": null}}
```

- `success` and the exit code agree: `0` done, `1` failed, `3` blocked by a guard
  (`data.blocked: true`), `2` bad command line.
- `data.error` says what happened in words, and for a guard, what to do next.
- With `--format json` a crash still prints JSON, and the traceback goes to stderr.
- `data.next` lists the follow-up commands on `add`, `remove`, `quarantine` and
  `ignore`.
- `data.bisync` on `resync` and `run` holds `cmd`, `returncode`, `stdout` and
  `stderr`. On failure the stderr has its ANSI colour codes removed, and
  `data.failed_paths` and `data.next_action` say what went wrong and what to do.
- `add --preflight` adds `data.content_probe`: `checked`, `readable`, and
  `unreadable` as `[{"path", "reason"}]`. It is separate from
  `risk.safe_for_bidirectional`, which is about names and policy.

`sync_tree.py` is the exception: it prints its own payload with a top-level
`schema` when it works, and the envelope when it fails.

## How a folder gets synced

Three layers, each built from the one before:

1. **Snapshot.** `monitor.db` holds one full scan of the disk and any number of
   newer partial scans on top. Refresh a folder with `scan cloud --path`.
2. **Policy.** `var/sync_policy.json` gives each path a mode: `bidirectional`,
   `download_only` or `disabled`. `quarantine` holds single files set aside with
   a reason, `ignore` holds patterns of generated files.
3. **Filters.** `<mirror>.bisync.filters` and `<mirror>.download.filters` are
   rendered from the policy. Never edit them.

bisync then keeps a baseline. Any change to the filter invalidates it: `run` exits
`3` until a `resync --apply` builds a new one. `add` and `remove` render the
filters themselves; `quarantine` and `ignore` need `render-filters --apply`.

`resync` is the dangerous one: the plan is free, the `--apply` writes to both
sides. `run` is the routine pass the timer makes, and it aborts when one side lost
more than `--max-delete` percent of the files (20 by default). That is a share,
not a count: three deletions of twelve files trip it, and a thousand of a
hundred thousand do not. `resync` deletes nothing: a file removed
on one side comes back from the other.

## Scanning

A subtree scan is the normal case, and a full scan is rare and needs the
person's agreement.

```bash
python3 ydm.py --backend rclone scan cloud --path /Folder
python3 ydm.py --backend rclone scan cloud --path /Folder --depth 1
python3 tools/cloud_delta.py --format json changes
```

Without a token, `ydm.py` needs `--backend rclone` on every cloud scan; the default
backend is the REST API. `cloud_delta` finds what to rescan in a few requests. It
misses moves and restores from Trash, because those keep their `modified` date.

### Long scans

A scan can be interrupted safely. On SIGTERM it saves to disk and can resume.

```bash
python3 ydm.py --backend rclone scan cloud --path /Folder --progress 2>&1 | tee var/scan.log &
SCAN_PID=$!

kill -TERM "$SCAN_PID"                                  # never SIGKILL
python3 ydm.py --format json report scan-list --limit 5
python3 ydm.py --format json report scan-progress --scan-id <ID>
python3 ydm.py --backend rclone scan cloud --resume --scan-id <ID> --progress
```

`status` is `success`, `started`, `interrupted` or `crashed`. Resume when it is one
of the last three and `.data.scan.progress.pending` is above zero, otherwise scan
again. `--config-profile test` checkpoints every 30 seconds instead of every
5 minutes, which is useful when testing the interrupt and resume cycle.

Progress is a stream of JSON lines:

```json
{"status": "progress", "scanned": 1250, "current": "/Folder/2024", "stats": {}}
```

## Reading the JSON

```bash
python3 ydm.py --format json report scan-list --limit 5 | jq '.data.scans[] | {id, type, status, files_count}'
python3 ydm.py --format json report scan-progress --scan-id <ID> | jq '.data.scan.progress'
python3 tools/sync_bisync.py doctor --format json | jq '.data | {healthy, failed, warned}'
python3 tools/sync_policy.py status --format json | jq '.data | {bidirectional_paths, download_only_paths, quarantine, ignore, resync_needed}'
python3 tools/sync_bisync.py status --format json | jq '.data | {state, current_filter_hash}'
python3 tools/sync_tree.py --path /Folder --depth 2 --format json --show-all | jq '.schema'
```
