*Читать по-русски: [README.ru.md](README.ru.md).*

# YDM - Yandex Disk Monitor

A tool for deep auditing of a Yandex Disk account: comparing the cloud
against a local mirror and tracking changes over time.

## Quick reference (shell aliases)

```bash
ydm-scan-cloud
ydm-scan-cloud-path /video
ydm-scan-local
ydm-tree
ydm-tree-path /video 3
ydm-sync-add /Projects/2024
ydm-sync-rm /Projects/2024
ydm-help
```

## Features

- 🔍 **Full cloud scan** - recursive walk of every file/folder via the Yandex Disk API
- 💾 **Local scan** - scans the local filesystem mirror
- 📊 **Cloud vs local diff** - finds discrepancies and sync problems
- 🔄 **Resumable scanning** - interrupt and resume a scan from where it left off
- ⚡ **Optimized for scale** - uses tmpfs for fast handling of large datasets
- 📈 **Detailed analytics** - duplicate detection, overly-long paths, structure analysis

## Requirements

- Python 3.6+
- One of two ways to talk to Yandex Disk:
  - **API backend (default)** — a Yandex Disk OAuth token (get one
    [here](https://yandex.ru/dev/disk/poligon/)); also needs the
    `yandex-disk` daemon for `scan local`/`report diff`/sync management.
  - **rclone backend** (`--backend rclone`) — an authorized remote in
    `rclone.conf` (`rclone config`), no daemon, no `.env` needed. For
    environments where the official `yandex-disk` daemon doesn't work
    (e.g. arm64) — details and the full toolset in
    [`tasks/rclone_backend/README.md`](tasks/rclone_backend/README.md).
    Note: that workstream was built for one specific Android/Termux
    device — see the disclaimer at the top of that file before assuming
    it works unmodified on yours.

## Installation

1. Clone the repository:
```bash
git clone <repository-url>
cd ydm
```

2. Configure access (pick one):
```bash
# Option A — API backend: .env file
echo "YANDEX_DISK_TOKEN=your_token_here" > .env

# Option B — rclone backend: remote in rclone.conf, no .env needed
rclone config   # storage> yandex
```

3. (Optional) Configure profiles in `ydm_config.json`:
```json
{
  "prod": {
    "cloud_batch_size": 500,
    "checkpoint_files_threshold": 10000,
    "checkpoint_time_sec": 300
  }
}
```

## Quick Start

### Initialize the database
```bash
python3 ydm.py init
```

### Scan the cloud
```bash
# Full scan with progress
python3 ydm.py scan cloud --progress

# Scan one folder
python3 ydm.py scan cloud --path "/Archive" --progress

# Resume an interrupted scan
python3 ydm.py scan cloud --resume --progress
```

### Scan the local filesystem
```bash
python3 ydm.py scan local --path /path/to/local/directory
```

### Get reports
```bash
# Status of recent scans
python3 ydm.py report status

# Cloud vs local diff
python3 ydm.py report diff

# Details of one scan
python3 ydm.py report scan-info --scan-id 5

# List all scans
python3 ydm.py report scan-list --limit 20
```

## Configuration

### Environment variables

Create a `.env` file at the project root:
```
YANDEX_DISK_TOKEN=your_oauth_token_here
```

### Configuration profiles

The project supports configuration profiles in `ydm_config.json`:

- **prod** (default) - stable operation with infrequent checkpoints
- **test** - fast testing with frequent checkpoints

Usage:
```bash
# Production profile (default)
python3 ydm.py scan cloud --progress

# Test profile (fast checkpoints)
python3 ydm.py --config-profile test scan cloud --progress
```

## Usage Examples

### Full scan workflow

```bash
# 1. Start a new scan
python3 ydm.py scan cloud --progress 2>&1 | tee scan.log &

# 2. Monitor progress
tail -f scan.log | jq -r 'select(.status=="progress") | "\(.scanned) files - \(.current)"'

# 3. Safely interrupt if needed (Ctrl+C or kill -TERM)
# Data is checkpointed to disk automatically

# 4. Resume later
python3 ydm.py scan cloud --resume --progress
```

### Cloud vs local diff

```bash
# 1. Scan the cloud
python3 ydm.py scan cloud --progress

# 2. Scan the local mirror
python3 ydm.py scan local --path /data/ya_disk

# 3. Compare results
python3 ydm.py report diff
```

### Finding problems

```bash
# Find files with long paths (>240 chars)
python3 ydm.py report long-paths --scan-id 5 --limit-chars 240

# Find duplicate files
python3 ydm.py report duplicates --scan-id 5

# Analyze scan integrity
python3 ydm.py report analyze-scan --scan-id 5
```

## Simple Sync Tools (transitional)

Transitional CLI utilities for managing sync via `exclude-dirs` and
viewing the sync tree (work off a `monitor.db` snapshot, no cloud scan
during execution).

### View the sync tree
```bash
# Collapsed tree (JSON by default)
python3 tools/sync_tree.py --path /Projects --depth 2

# Full tree with branches (text)
python3 tools/sync_tree.py --path /Projects --depth 2 --format text --text-tree --show-all

# Without the text header
python3 tools/sync_tree.py --format text --text-tree --no-text-header

# Disable the local scan and sync_percent
python3 tools/sync_tree.py --no-local-scan --no-sync-percent
```

### Managing exclude-dirs
```bash
# List exclude-dirs
python3 tools/sync_exclude.py list

# Dry-run: include a subfolder
python3 tools/sync_exclude.py add --path /Projects/2024

# Apply the change
python3 tools/sync_exclude.py add --path /Projects/2024 --apply

# Exclude a folder
python3 tools/sync_exclude.py remove --path /Projects/2024 --apply

# Text output without the header
python3 tools/sync_exclude.py add --path /Projects/2024 --format text --no-text-header

# Skip the daemon restart and local scan
python3 tools/sync_exclude.py add --path /Projects/2024 --apply --no-restart-daemon --no-local-scan
```

### Managing sync without a daemon (`--backend rclone`)

For environments without `yandex-disk` (see [Rclone Backend](tasks/rclone_backend/README.md)),
`tools/sync_exclude.py` is replaced by `tools/sync_filters.py` — same UX
(dry-run by default, `--apply` to apply), but instead of editing
`config.cfg` + restarting the daemon, it edits an rclone filter-file and
materializes via `rclone copy`:

```bash
# List folders currently included in sync
python3 tools/sync_filters.py list --local-root /path/to/local/mirror

# Dry-run: include a folder
python3 tools/sync_filters.py add --path /Projects --local-root /path/to/local/mirror

# Apply — materializes locally via rclone copy
python3 tools/sync_filters.py add --path /Projects --local-root /path/to/local/mirror --apply

# Remove from sync; --delete-local only clears local contents after a
# clean `rclone check` (0 differences)
python3 tools/sync_filters.py remove --path /Projects --local-root /path/to/local/mirror --apply --delete-local
```

`tools/sync_tree.py` also supports `--backend rclone` (default is `api`,
unchanged behavior): with `--backend rclone` the tree is built from the
filter-file instead of `config.cfg`.

JSON contract (schema versions):
- `sync_tree` → `"schema": "sync_tree:v1"`
- `sync_exclude` → `"schema": "sync_exclude:v1"`
- `sync_filters` → `"schema": "sync_filters:v1"`

Notes:
- By default `sync_tree` runs a local scan and computes `sync_percent`.
- By default `sync_exclude --apply` restarts the daemon and runs a local scan.
- `sync_filters --apply` never restarts anything (no daemon) — it runs `rclone copy` directly.

## Shell aliases (`~/.bashrc`)

### What's added
Aliases and functions for common workflows, added to `~/.bashrc`:
- `ydm-scan-cloud` — full cloud scan
- `ydm-scan-cloud-path <path>` — cloud scan of one folder
- `ydm-scan-local` — local scan of `/data/ya_disk`
- `ydm-tree` — sync tree (text + branches)
- `ydm-tree-path <path> [depth]` — sync tree for one folder, with depth
- `ydm-sync-add <path>` — add a folder to sync
- `ydm-sync-rm <path>` — remove a folder from sync
- `ydm-help` — short cheat sheet

### Important
- `ydm-sync-add` and `ydm-sync-rm` **run with `--apply` directly**.
  The `exclude-dirs` change is applied immediately, followed by a daemon
  restart and a local scan (the `sync_exclude` defaults).

### Examples
```bash
ydm-tree-path /video 3
ydm-scan-cloud-path /Projects
ydm-sync-add /Projects/2024
```

### Applying changes
```bash
source ~/.bashrc
```
## Documentation

- **[PROJECT_YD_MONITOR.md](docs/PROJECT_YD_MONITOR.md)** - Full project documentation, architecture, implementation details
- **[QUICKSTART_AI.md](docs/QUICKSTART_AI.md)** - Quick start for AI assistants and automation
- **[USAGE_EXAMPLES.md](docs/USAGE_EXAMPLES.md)** - Additional usage examples
- **[Sync Manager](tasks/sync_manager/README.md)** - Transitional sync_tree/sync_exclude tools and Sync Manager plans
- **[Rclone Backend](tasks/rclone_backend/README.md)** - Alternative to the `yandex-disk` daemon for environments without it (arm64/Android): `RcloneBackend`, `sync_filters.py`, junk cleanup via rclone

## Project layout

Main files:

- `ydm.py` — the core CLI tool (DB init, scans, reports)
- `ydm_config.json` — profile configuration (prod/test)
- `monitor.db` — the main SQLite database (scan results)
- `README.md`, `LICENSE`, `.env.example` — docs and a sample config

Top-level folders:

- `docs/` — general project documentation
  - `PROJECT_YD_MONITOR.md` — architecture and implementation details
  - `QUICKSTART_AI.md` — quick start for AI/automation
  - `USAGE_EXAMPLES.md` — usage examples
  - `issues/` — historical reports and issue write-ups
  - `long_names/` — supporting materials for the long-paths task
- `tasks/` — tasks/subprojects built on the core
  - `tasks/junk/` — junk-cleanup task:
    - `plan_cleanup.py` — generates a deletion plan (`var/junk_list.txt`)
    - `run_cleanup.py` — executes the plan (`--backend api|rclone`, `var/deleted.log`)
    - `smart_clean.py` — combined analyze+clean script
    - `analyze_junk.py`, `CLEANUP_GUIDE.md`, `JUNK_REPORT.md` — cleanup analytics and docs
  - `tasks/long_names/` — long-paths task:
    - `ISSUE_LONG_FILENAMES.md`, `RESULTS_AND_PLAN.md`
  - `tasks/rclone_backend/` — `yandex-disk` daemon alternative via rclone
    (for environments like arm64 where the official client doesn't work)
- `tools/` — supporting utilities
  - `gen_exclude_list.py` — generates an `exclude-dirs=` config string for Yandex Disk
  - `sync_tree.py` — sync tree from a snapshot (JSON/text; `--backend api|rclone`)
  - `sync_exclude.py` — add/remove/list for `exclude-dirs` (daemon, dry-run by default)
  - `sync_filters.py` — add/remove/list for the rclone filter-file (no daemon, dry-run by default)
  - `sync_common.py` — shared code for the sync utilities
- `tests/` — test scripts:
  - `test_scan.sh` — integration test of scanning on tmpfs
  - `test_ydm_fixes.sh` — regression suite for `ydm.py`
- `assets/` — media/diagrams:
  - `poligon_endpoints.png` — API endpoint diagram (historical)
- `var/` — runtime data and artifacts:
  - `junk_list.txt` — the current cleanup plan
  - `deleted.log` — log of actually-deleted paths
  - other files (`*.db`, `*.json`, `*.old`) — temporary/diagnostic data

## Architecture

The project uses a two-tier storage design to balance performance and safety:

1. **RAM DB (tmpfs)** - temporary in-memory storage for fast scanning
2. **Disk DB (monitor.db)** - persistent storage with periodic checkpoints

See [PROJECT_YD_MONITOR.md](docs/PROJECT_YD_MONITOR.md) for more on the architecture.

## Command Reference

### Global Flags
- `--db-path PATH` - path to the database (default: `monitor.db`)
- `--format {text|json}` - output format (default: `text`)
- `--config-profile {prod|test}` - configuration profile (default: `prod`)
- `--backend {api|rclone}` - data source for `scan meta`/`scan cloud`
  (default: `api`, requires `YANDEX_DISK_TOKEN`; `rclone` — via
  `rclone.conf`, see [`tasks/rclone_backend/README.md`](tasks/rclone_backend/README.md))

### Scan Commands
- `scan meta` - quick disk metadata
- `scan cloud [--path PATH] [--progress] [--resume] [--scan-id ID]` - scan the cloud
- `scan local [--path PATH]` - scan the local filesystem

### Report Commands
- `report status` - recent scans
- `report diff` - cloud vs local diff
- `report scan-list [--limit N]` - list all scans
- `report scan-info --scan-id ID` - scan details
- `report scan-progress --scan-id ID` - scan progress
- `report long-paths --scan-id ID [--limit-chars N]` - files with long paths
- `report duplicates --scan-id ID [--by-hash|--by-name]` - find duplicates
- `report analyze-scan --scan-id ID` - integrity analysis

Full command list: `python3 ydm.py --help`

## License

MIT License - see the [LICENSE](LICENSE) file for details.

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md).

## Notes

- The project uses only the Python standard library (stdlib), no external dependencies
- All data is stored locally in a SQLite database
- Tokens and secrets are never committed to the repository (use a `.env` file)
