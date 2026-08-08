# YDM Menu — Design

*Дата:* 2026-08-08
*Зависимости:* `sync_policy`, `sync_tree` (+ `_policy`, `_cloud`), `sync_bisync`,
`sync_filters`, `sync_common`, `~/.bashrc`

## Principles

1. **Dual interface:** human menu + agent CLI coexist; menu wraps, never replaces.
2. **Policy-first:** все add/remove идут через `var/sync_policy.json`, filters — derived.
3. **Numbers, not paths:** кириллица только в списках, не в input.
4. **Stdlib only:** Python 3.9+, без curses/prompt_toolkit (Termux/proot).
5. **Testable:** core logic в functions; stdin mockable; subprocess для rclone/bisync.
6. **Short screens:** ≤20 строк на экран; Enter = default; `q` = назад.

---

## Architecture

```text
                    ┌─────────────────┐
                    │  ydm / ydm-menu │  ~/.bashrc alias
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │ tools/ydm_menu.py│  REPL loop, screens
                    └────────┬────────┘
         ┌───────────────────┼───────────────────┐
         │                   │                   │
  ┌──────▼──────┐   ┌────────▼────────┐  ┌──────▼──────┐
  │ ydm_menu_   │   │ ydm_menu_actions │  │ ydm_menu_   │
  │ screens.py  │   │ .py              │  │ orphans.py  │
  │ (render UI) │   │ (orchestration)  │  │ (discover)  │
  └──────┬──────┘   └────────┬────────┘  └──────┬──────┘
         │                   │                   │
         └───────────────────┼───────────────────┘
                             │
    sync_policy │ sync_tree* │ sync_bisync │ sync_filters │ sync_common
```

### Module responsibilities

| Module | Role |
|--------|------|
| `ydm_menu.py` | CLI: `--db-path`, `--local-root`, REPL, `--non-interactive` for tests |
| `ydm_menu_config.py` | Defaults from env / `YDM_*` / argparse |
| `ydm_menu_screens.py` | Pure render: header, menu, lists, confirmations |
| `ydm_menu_prompts.py` | read line, parse int/ints, yes/no, validate |
| `ydm_menu_status.py` | Aggregate status (bisync + policy + lock + warnings) |
| `ydm_menu_orphans.py` | Walk tree / FS → list of orphan entries |
| `ydm_menu_actions.py` | add/remove/run/resync/scan — call existing tools APIs |

Import existing tool **functions**, not shell aliases.

---

## Entry points

```bash
ydm                          # interactive REPL
ydm-menu                     # alias
python3 tools/ydm_menu.py    # direct
python3 tools/ydm_menu.py --once add-orphans   # optional script mode (phase 5)
```

### Flags

```text
--db-path PATH
--local-root PATH
--policy-path PATH
--remote NAME              default: yandex
--non-interactive          for tests / CI smoke
--plain                    no decorative lines
```

---

## Main screen

```
YDM Sync
──────────────────────────────────────
Status: OK        last bisync: 02:38
Lock: no          resync: not needed

Synced: Books/Math/База[B]  DAO[B]  +3 more

 1  Show sync tree
 2  Add folder from cloud
 3  Add LOCAL folder to sync     ← orphans [L]
 4  Remove folder from sync
 5  Run bisync now
 6  Resync baseline (after path changes)
 7  Cloud scan (update snapshot)
 8  Detailed status
 9  Help (short)
 q  Quit

>
```

**Header** всегда: overall status, last run, resync hint if needed.

`+3 more` — если paths > 2, полный список в пункте 8.

---

## Screen flows

### 1. Show sync tree

```
Tree root: [/] default
 1  / (all, depth 4)
 2  /Books/Math (depth 3)
 3  Custom path...
 0  Back

Depth [4]:
```

Вызывает `sync_tree` programmatically или subprocess с `--format text --text-tree`.
Показ через **pager** (`less`) если tty interactive, иначе print.

### 2. Add from cloud

Reuse `ydm-sync-pick` logic in Python:

1. Ask parent path (numbered recent: `/Books/Math`, `/DAO`, … + custom)
2. `rclone lsf yandex:parent --dirs-only --max-depth 1`
3. Numbered list → pick
4. Mode: `[1] bidirectional  [2] download-only  [0] cancel`
5. `sync_policy inspect` → if blocked, sub-dialog (see Risk)
6. `add_policy_path(apply=True)` + `render_filters(apply=True)`
7. Post-action: resync offer if bidirectional

### 3. Add LOCAL folder (orphans) — ключевой flow

**Discovery** (`ydm_menu_orphans.py`):

```python
@dataclass
class OrphanEntry:
    cloud_path: str       # /Books/Math/АнГем
    rel_path: str         # Books/Math/АнГем
    local_bytes: int
    cloud_file_count: int | None
    in_cloud_snapshot: bool
```

Algorithm:

1. Build sync tree from `/` depth 4 (or walk local FS under `local_root`).
2. Collect nodes where `display_marker == "[L]"` OR (`local_state == orphan`).
3. Dedupe by `rel_path`, sort by path.
4. Optional: hide empty dirs, min size filter.

**UI:**

```
Local folders not in sync:

 1  Books/Math/АнГем           393 MB   cloud: 116 files
 2  ...

Enter numbers (1,2) or 'all' or 0=back:
```

Mode selection (single letter ok):

```
Mode: 1=bidirectional  2=download-only  0=cancel
> 1
```

For each selected path → inspect → add → render filters.

Batch: один resync prompt в конце.

### 4. Remove from sync

List only policy paths:

```
Remove from sync:

 1  [B] Books/Math/База
 2  [B] DAO
 ...

> 2

Remove DAO from sync?
  - stop bisync for this path
  - optional: delete local copy

Delete local files? [y/N]
Confirm type path name: DAO
```

**Implementation (policy-first):**

```text
sync_policy remove --apply
sync_policy render-filters --apply
sync_filters remove --apply --delete-local   (only if user confirmed delete)
```

Fix G4: remove must update policy (new requirement for `ydm-sync-rm` too).

### 5–6. Bisync run / resync

- Show dry-run plan first for resync (existing `sync_bisync resync` without `--apply`)
- Confirm: `Proceed? [y/N]`
- Run with `--apply`
- Show tail of log / returncode

If lock held → «bisync already running (pid …), wait».

### 7. Cloud scan

```
Scan cloud:
 1  /Books/Math
 2  /DAO
 3  Full disk (slow)
 4  Custom path
 0  Back
```

Subprocess `ydm.py scan cloud --backend rclone --path … --progress` with note:
«Can take long; Ctrl+C safe».

### 8. Detailed status

Human rendering of:

- `sync_bisync status`
- `sync_policy status`
- filter hash mismatch
- recent log lines (bisync.log tail)

---

## Risk block dialog

When `inspect` returns `safe_for_bidirectional: false`:

```
Cannot add as bidirectional: /pro/agents

Risk: android_incompatible_names (13 files)
  example: Agents Week 2026 | ...

 1  Add as download-only instead
 2  Run cloud scan and retry
 3  Force bidirectional anyway (--force-risk)  [expert]
 0  Cancel
```

Expert option requires typing `yes`.

---

## Post-action: resync orchestration

Central function `offer_resync_if_needed()` replaces bash `_ydm_offer_resync`:

```python
def resync_needed(status: MenuStatus) -> bool:
    return status.policy_filter_resync_needed or status.bisync_resync_needed

def after_policy_change(mode: str, status: MenuStatus) -> None:
    if mode != "bidirectional":
        return
    if resync_needed(status):
        if confirm("Filters changed. Run bisync resync now?", default=True):
            run_resync(apply=True)
        else:
            show_warning("Scheduled bisync may fail until resync is done.")
```

Also detect `filter_mismatch` from sync_tree policy context.

---

## Orphan API (shared with agents)

Export from `ydm_menu_orphans.py` (or `sync_tree_orphans.py` if reused):

```python
def list_orphan_paths(
    db_path: str,
    local_root: str,
    policy_path: str,
    *,
    root: str = "/",
    max_depth: int = 5,
) -> List[OrphanEntry]:
    ...
```

JSON mode for agents:

```bash
python3 tools/ydm_menu.py --non-interactive orphans --format json
```

Schema: `ydm_menu_orphans:v1`

---

## Menu status object

```python
@dataclass
class MenuStatus:
    overall: str              # OK | BUSY | NEEDS_RESYNC | CHECK
    last_run_at: str | None
    last_status: str | None
    lock_held: bool
    lock_pid: int | None
    bidirectional: list[str]
    download_only: list[str]
    resync_needed: bool
    filter_mismatch: bool
    warnings: list[str]
```

Built by `ydm_menu_status.load_status()` calling existing Python APIs.

---

## Termux UX rules

| Rule | Implementation |
|------|----------------|
| Narrow width | wrap at 72 cols; truncate middle of long paths |
| No ANSI required | plain ASCII default; optional `--plain` |
| Pager for long output | `less -F` when stdout is tty |
| Default Enter | `(default: 4)` shown in prompts |
| Quit consistency | `q`, `0`, empty on back screens |
| Cyrillic display | OK in lists; never ask to type Cyrillic path |
| Error recovery | try/except → «Press Enter» → back to menu, no stack trace |

---

## Relationship to bash aliases

After menu stable:

| Alias | Fate |
|-------|------|
| `ydm` | **points to menu** (new) |
| `ydm-menu` | alias to same |
| `ydm-cli` or keep `ydm-sync-add` | agent/human expert commands unchanged |
| `ydm-tree`, `ydm-sync-state` | unchanged; also callable from menu |
| `_ydm_offer_resync` | **deprecated** → Python `offer_resync_if_needed` |
| `ydm-sync-rm` | **fix** to policy-first remove (phase 2) |

**Breaking change note:** `ydm` as menu name shadows «ydm» generically — acceptable;
project brand is YDM; subcommands stay `ydm-scan-cloud`, etc.

Alternative: primary `ydm-menu`, `ydm` stays help — DESIGN chooses **`ydm` = menu**
because user asked for simplicity; `ydm-help` remains.

---

## Testing strategy

| Layer | Tests |
|-------|-------|
| `ydm_menu_orphans` | synthetic tree + tmp local dirs |
| `ydm_menu_prompts` | parse "1,2", yes/no defaults |
| `ydm_menu_actions` | mock sync_policy add/remove (temp policy file) |
| `ydm_menu_status` | fixture JSON from bisync/policy |
| Integration | `--non-interactive` scripted session |
| CI | `python3 tools/ydm_menu.py --help`; orphans JSON smoke |

No live rclone in CI.

---

## Security / safety

- Remove: double confirm + type path basename for `[B]` paths
- Resync: dry-run shown before `--apply`
- `--force-risk`: require typing `yes`
- Never log tokens; paths only

---

## Future extensions (post-v1)

- Rename guard submenu (wrap `sync_rename detect`)
- Termux `notify` on bisync complete from menu
- «Watch mode» refresh status every N sec (optional)

---

## File layout (implementation)

```text
tools/ydm_menu.py
tools/ydm_menu_config.py
tools/ydm_menu_screens.py
tools/ydm_menu_prompts.py
tools/ydm_menu_status.py
tools/ydm_menu_orphans.py
tools/ydm_menu_actions.py
tests/test_ydm_menu.py
tests/test_ydm_menu_orphans.py
tasks/ydm_menu/          ← this doc
```

Single entry `ydm_menu.py` if modules feel overkill — but BACKLOG assumes split for testability (user asked full implementation, no corners cut).
