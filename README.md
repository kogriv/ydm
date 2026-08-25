*Читать по-русски: [README.ru.md](README.ru.md).*

# YDM - Yandex Disk Monitor

A tool for deep auditing of a Yandex Disk account: comparing the cloud
against a local mirror and tracking changes over time.

## Quick reference

Straight after a clone, with nothing configured:

```bash
python3 ydm.py --help
python3 tools/sync_tree.py --help
```

Once you have a token or an rclone remote (see
[Installation](#installation)) and a database:

```bash
python3 ydm.py init
python3 ydm.py scan cloud --progress
python3 ydm.py scan local --path /path/to/your/mirror
python3 ydm.py report diff
```

The shell aliases below wrap those with your paths already filled in.
They live in [`tools/aliases.sh`](tools/aliases.sh) — source it once:

```bash
export YDM_LOCAL_ROOT="$HOME/YandexDisk"   # wherever your mirror is
echo "source $(pwd)/tools/aliases.sh" >> ~/.bashrc
```

```bash
ydm-menu
ydm-scan-cloud
ydm-scan-cloud-path /video
ydm-scan-local
ydm-tree
ydm-tree-path /video 3
ydm-sync-add /Projects/2024
ydm-sync-rm /Projects/2024
ydm-help
```

> `ydm-sync-add` and `ydm-sync-rm` **apply immediately** — on the
> `yandex-disk` daemon they rewrite `exclude-dirs` and restart it, which
> starts real syncing or real removal of local copies. There is no dry run
> in the alias; use `python3 tools/sync_policy.py add …` without `--apply`
> for that. See
> [`docs/incidents/yandex-books-delete-2026-08-14.md`](docs/incidents/yandex-books-delete-2026-08-14.md)
> for what this looks like when it goes wrong.

For rclone/bisync on Android, use the policy-aware layer to separate
download-only mirrors from bidirectional paths:

```bash
python3 tools/sync_policy.py status --local-root /sdcard/Download/ya_disk
python3 tools/sync_policy.py inspect --path /pro/agents --local-root /sdcard/Download/ya_disk
python3 tools/sync_policy.py render-filters --local-root /sdcard/Download/ya_disk --apply
python3 tools/sync_rename.py plan --local-root /sdcard/Download/ya_disk --old /DAO/a.txt --new /DAO/b.txt
python3 tools/sync_rename.py apply --local-root /sdcard/Download/ya_disk --old /DAO/a.txt --new /DAO/b.txt
```

On Ubuntu with the official `yandex-disk` daemon the same `sync_policy.py`
commands work as well: they manage `exclude-dirs=` in
`~/.config/yandex-disk/config.cfg`. See
[`tasks/sync_unification/README.md`](tasks/sync_unification/README.md) for
one CLI across both environments.

## Features

- 🔍 **Full cloud scan** - recursive walk of every file/folder via the Yandex Disk API
- 💾 **Local scan** - scans the local filesystem mirror
- 📊 **Cloud vs local diff** - finds discrepancies and sync problems
- 🔄 **Resumable scanning** - interrupt and resume a scan from where it left off
- ⚡ **Optimized for scale** - uses tmpfs for fast handling of large datasets
- 📈 **Detailed analytics** - duplicate detection, overly-long paths, structure analysis

## Requirements

- Python 3.9+ (the `sync_*` tools use `argparse.BooleanOptionalAction`,
  added in 3.9 — tested in CI on 3.9/3.11/3.13)
- One of two ways to talk to Yandex Disk:
  - **API backend (default)** — a Yandex Disk OAuth token (get one
    [here](https://yandex.ru/dev/disk/poligon/)); also needs the
    `yandex-disk` daemon for `scan local`/`report diff`/sync management.
  - **rclone backend** (`--backend rclone`) — an authorized remote in
    `rclone.conf` (`rclone config`), no daemon, no `.env` needed. For
    environments where the official `yandex-disk` daemon doesn't work
    (e.g. arm64) — details and the full toolset in
    [`tasks/rclone_backend/README.md`](tasks/rclone_backend/README.md).
    Note: that workstream was built for one specific Android device, and
    for a proot-Debian container *inside* Termux rather than for Termux
    itself — the full arrangement, including the scheduled job, is written
    out in [`docs/ANDROID_SETUP.md`](docs/ANDROID_SETUP.md).

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

# Refresh just the files sitting directly in the disk root (~1 second),
# without the full walk that "--path /" would mean
python3 ydm.py scan cloud --path / --depth 1

# Resume an interrupted scan
python3 ydm.py scan cloud --resume --progress
```

`--depth N` walks N levels below `--path` and stops. It is always a partial
update: a depth-limited scan never becomes the composite snapshot's base, so
it can refresh a folder without the rest of the disk falling out of the
snapshot.

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

## Shell aliases (`tools/aliases.sh`)

### Installing them

```bash
export YDM_LOCAL_ROOT="$HOME/YandexDisk"   # your local mirror
echo "source /path/to/ydm/tools/aliases.sh" >> ~/.bashrc
source ~/.bashrc
```

Three variables control everything; set any of them before sourcing:

| Variable | Default | Meaning |
|---|---|---|
| `YDM_ROOT` | the checkout the script sits in | project directory |
| `YDM_DB` | `$YDM_ROOT/monitor.db` | database |
| `YDM_LOCAL_ROOT` | **none** | your local mirror |

`YDM_LOCAL_ROOT` has no default on purpose: the mirror is wherever you put
it, and guessing would mean scanning or syncing the wrong directory.
Commands that need it stop and say so.

### What you get
- `ydm` — the base command with `--db-path` already set
- `ydm-scan-cloud` — full cloud scan
- `ydm-scan-cloud-path <path>` — cloud scan of one folder
- `ydm-scan-local` — local scan of `$YDM_LOCAL_ROOT`
- `ydm-menu` — interactive sync UI (daemon or rclone)
- `ydm-tree` — sync tree (text + branches)
- `ydm-tree-path <path> [depth]` — sync tree for one folder, with depth
- `ydm-sync-add <path> [mode]` — include a folder; mode is `bidirectional`
  (default), `download_only` or `disabled`
- `ydm-sync-rm <path>` — exclude a folder
- `ydm-help` — the cheat sheet, including the paths currently in effect

If `ydm-sync-add` prints `BLOCKED` with `Risk: path_not_found`, the path exists
in the cloud but is missing from the current YDM cloud snapshot. Refresh that
snapshot first with `ydm-scan-cloud-path <path>`, then retry.

### Termux/proot scroll

Checked on the device (Android 15, 2026-08-25): finger swipe scrolls the
output, `ydm-help` fits the screen without breaking words, and leaving `less`
does not break scrolling. For long output a pager is still often nicer:

```bash
ydm-help | less
```

Inside `less`:

- `q` — quit
- `Space` / `b` — page down / page up
- `j` / `k` — line down / line up
- `/text` — search

If scrolling *is* broken, the symptom is **touching the screen printing junk
characters into the command line** — the terminal has mouse reporting enabled
and encodes each tap as an escape sequence. Clear it with:

```bash
printf '\033[?1000l\033[?1002l\033[?1003l\033[?1006l\033[?1015l'
tput rmcup; stty sane
```

(Earlier versions of this section described the symptom as swipes acting like
up/down arrows, and called the fix `termux-scroll-fix` as though Termux
shipped it. Neither was right: the three lines above are the whole fix, and
this project does not ship a wrapper for them.)

### Important
- `ydm-sync-add` and `ydm-sync-rm` **run with `--apply` directly**.
  The `exclude-dirs` change is applied immediately, followed by a daemon
  restart and a local scan (the `sync_exclude` defaults). For a dry run,
  call `python3 tools/sync_policy.py add --path <path> --mode <mode>`
  without `--apply`.

### Examples
```bash
ydm-tree-path /video 3
ydm-scan-cloud-path /Projects
ydm-sync-add /Projects/2024
```
## Documentation

> **A note on language.** This README, `docs/` and the code are in English.
> The `tasks/` tree — gap analyses, design docs and backlogs, about two thirds
> of the writing here — is in Russian, and stays that way by choice rather than
> by neglect: Yandex Disk is a Russian service and so are most of the people
> who audit one. The English side is meant to stand on its own; if something on
> it is unclear because the reasoning lives only in `tasks/`, that is a bug
> worth reporting.

- **[PROJECT_YD_MONITOR.md](docs/PROJECT_YD_MONITOR.md)** - Full project documentation, architecture, implementation details
- **[QUICKSTART_AI.md](docs/QUICKSTART_AI.md)** - Quick start for AI assistants and automation
- **[ANDROID_SETUP.md](docs/ANDROID_SETUP.md)** - The whole Android arrangement: proot-Debian inside Termux, bind mounts, the scheduled bisync job, and what shared storage refuses to store
- **[USAGE_EXAMPLES.md](docs/USAGE_EXAMPLES.md)** - Additional usage examples
- **[Sync Tree v2](tasks/sync_tree/README.md)** - Policy-aware `ydm-tree` (`[B]`/`[D]`/`[L]` markers, orphan paths)
- **[YDM Menu](tasks/ydm_menu/README.md)** - Interactive sync UI for humans (`ydm`; agents keep `ydm-sync-*` CLI)
- **[Sync Manager](tasks/sync_manager/README.md)** - Transitional sync_tree/sync_exclude tools and Sync Manager plans
- **[Unified Sync Interface](tasks/sync_unification/README.md)** - One CLI (`ydm-menu`, `ydm-sync-add`, `ydm-tree`) for both the `yandex-disk` daemon and the `rclone` backend, with automatic backend detection
- **[Rclone Backend](tasks/rclone_backend/README.md)** - Alternative to the `yandex-disk` daemon for environments without it (arm64/Android): `RcloneBackend`, `sync_filters.py`, junk cleanup via rclone
- **[Delta Scan](tasks/delta_scan/README.md)** - Proposed cheap change detection (disk revision + `-modified` sweep + trash) so the composite snapshot knows where it is stale
- **[Diff Correctness](tasks/diff_correctness/README.md)** - Why `report diff` never matched anything (cloud stores `/A/B`, local stores `A/B`), and the single comparison path that replaced three copies
- **[Smart Diff](tasks/smart_diff/README.md)** - How `report diff` builds a composite snapshot (full scan + newer partial scans) instead of just comparing the two latest scans
- **[CHANGELOG.md](CHANGELOG.md)** - Notable changes, newest first
- **[Known Issues](docs/KNOWN_ISSUES.md)** - Current limitations that aren't fixed yet

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
  - `ANDROID_SETUP.md` — running this on Android, end to end
  - `USAGE_EXAMPLES.md` — usage examples
  - `KNOWN_ISSUES.md` — current, unfixed limitations
- `tasks/` — tasks/subprojects built on the core
  - `tasks/junk/` — junk-cleanup task:
    - `plan_cleanup.py` — generates a deletion plan (`var/junk_list.txt`)
    - `run_cleanup.py` — executes the plan (`--backend api|rclone`, `var/deleted.log`)
    - `smart_clean.py` — combined analyze+clean script
    - `analyze_junk.py`, `CLEANUP_GUIDE.md` — cleanup analytics and docs
  - `tasks/smart_diff/` — composite-snapshot diff (see Documentation above)
  - `tasks/long_names/` — AI-assisted long-filename renaming, unfinished
    proof-of-concept (`smart_renamer.py`) — see its README for status
  - `tasks/rclone_backend/` — `yandex-disk` daemon alternative via rclone
    (for environments like arm64 where the official client doesn't work)
- `tools/` — supporting utilities
  - `gen_exclude_list.py` — generates an `exclude-dirs=` config string for Yandex Disk
  - `sync_tree.py` — sync tree from a snapshot (JSON/text; `--backend api|rclone`)
  - `sync_exclude.py` — add/remove/list for `exclude-dirs` (daemon, dry-run by default)
  - `sync_filters.py` — add/remove/list for the rclone filter-file (no daemon, dry-run by default)
  - `sync_common.py` — shared code for the sync utilities
  - `termux/` — the Android automation: `job_run.sh` (the container half of the
    scheduled sync, runnable by hand), `ydm_bisync_job.sh` (the Termux half),
    `install_job.sh` (register it) — see
    [`docs/ANDROID_SETUP.md`](docs/ANDROID_SETUP.md)
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
- `report diff` - cloud vs local diff (composite snapshot by default, see [Smart Diff](tasks/smart_diff/README.md))
- `report scan-list [--limit N]` - list all scans
- `report scan-info --scan-id ID` - scan details
- `report scan-progress --scan-id ID` - scan progress
- `report long-paths --scan-id ID [--limit-chars N]` - files with long paths
- `report duplicates --scan-id ID [--by-hash|--by-name]` - find duplicates
- `report clean-duplicates [--scan-id ID]` - remove duplicate rows left by an older bug (see [CHANGELOG.md](CHANGELOG.md))
- `report prune [--apply] [--vacuum] [--keep-local N] [--keep-root-scans N]` - delete scans the composite no longer needs; dry-run by default, and it lists what it protects and why
- `report analyze-scan --scan-id ID` - integrity analysis
- `report full-scan-info` / `report full-scan-candidates` - inspect which scan is used as the composite-diff base and why

Full command list: `python3 ydm.py --help`

## License

MIT License - see the [LICENSE](LICENSE) file for details.

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md).

## Known Issues

See [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md) — currently one: SIGTERM
sent during scan initialization (before the main loop starts) can hang
the process; use `kill -9` if that happens.

## Notes

- The project uses only the Python standard library (stdlib), no external dependencies
- All data is stored locally in a SQLite database
- Tokens and secrets are never committed to the repository (use a `.env` file)
