# Sync Tree v2 — Design

*Дата:* 2026-08-08
*Зависимости:* `tools/sync_tree.py`, `tools/sync_policy.py`, `tools/sync_common.py`,
`~/.bashrc` aliases

## Goals

1. **Single visual answer:** «что в облаке, что локально, что и как синкается»
2. **Policy-first:** `var/sync_policy.json` — источник режимов; filter-files —
   derived artifacts (как в bisync layer)
3. **Robust on partial scans:** дерево строится даже если последний cloud scan
   покрывает только `/Books/Math`
4. **Termux-friendly:** короткие строки, legend, без обязательного local scan
5. **Backward compatible:** `sync_tree:v1` JSON сохраняется; v2 — opt-in или
   default с migration note

## Non-goals

- Не дублировать risk analyzer logic — reuse `sync_policy.inspect`
- Не менять формат `sync_policy.json`
- Не требовать full cloud scan для каждого просмотра дерева

---

## Data sources (priority order)

```text
┌─────────────────────────────────────────────────────────┐
│ 1. var/sync_policy.json     modes, reasons, updated_at  │
│ 2. monitor.db composite     cloud tree structure        │
│ 3. latest local scan        materialization / orphan    │
│ 4. *.bisync.filters         verify consistency (hash)   │
│ 5. filesystem (optional)    orphan detect if no scan    │
└─────────────────────────────────────────────────────────┘
```

Policy path membership:

```python
def policy_mode_for_path(path: str, policy: dict) -> str | None:
    """Longest-prefix match in policy['paths']."""
    # /Books/Math/База → bidirectional if entry "Books/Math/База"
    # ancestor Books/Math → None unless explicit entry
```

Filter-file используется для **consistency check** (hash vs `render-filters`),
не как primary source of truth.

---

## Node model (schema `sync_tree:v2`)

```json
{
  "schema": "sync_tree:v2",
  "header": {
    "root_path": "/Books/Math",
    "depth": 3,
    "policy_path": "/root/notes/pro/ydm/var/sync_policy.json",
    "cloud_snapshot": {
      "base_scan_id": 2426,
      "folder_updates": {},
      "freshness_warning": null
    },
    "local_scan_id": 2420,
    "legend": ["[B] bidirectional", "[D] download-only", "[L] local orphan", "[.] cloud only"]
  },
  "root": {
    "path": "/Books/Math",
    "name": "Math",
    "markers": {
      "display": "[P]",
      "policy_mode": null,
      "local_state": "partial",
      "cloud_state": "present"
    },
    "sync_percent": 4.0,
    "in_policy": false,
    "policy_entry": null,
    "children": []
  }
}
```

### Marker semantics (text output)

Компактный display marker (1–2 символа для узкого Termux):

| Display | policy_mode | local materialized | Meaning |
|---------|-------------|-------------------|---------|
| `[B]` | bidirectional | yes | В policy + bisync + локально |
| `[B~]` | bidirectional | partial | В policy, локально не всё |
| `[B?]` | bidirectional | no | В policy, локально пусто |
| `[D]` | download_only | yes | Только copy, не bisync |
| `[D?]` | download_only | no | В policy download, не скачано |
| `[L]` | null | yes | **Local orphan** — на диске, не в policy |
| `[.]` | null | no | Только облако |
| `[P]` | null | partial | Предок synced path (как v1 partial) |
| `[X]` | disabled | — | В policy как disabled |

v1 mapping (deprecated, document in legend):

- `[S]` → `[B]` or `[D]` (split by mode)
- `[-]` → `[.]` or `[L]`

### `local_state` derivation

```text
local_count == 0 && dir exists on disk     → materialized (empty dir) or missing
local_count > 0                            → materialized
local_count > 0 && not in policy           → orphan
cloud_count > 0 && local_count == 0        → missing (for in_policy paths → [B?]/[D?])
```

Orphan detection:

1. Prefer local scan DB counts under path prefix
2. Fallback: `os.path.isdir(local_root + rel_path)` when scan skipped

---

## Cloud tree building (robustness)

### Problem

`fetch_child_dirs()` requires `type='dir'` rows. Partial rclone scans may only
persist files.

### Solution: dual source child discovery

```python
def fetch_child_names(storage, scan_id, parent_path) -> List[str]:
    dirs = fetch_child_dirs(...)  # existing
    if dirs:
        return dirs
    # Fallback: infer dirs from file parent_path prefixes
    return infer_dirs_from_files(storage, scan_id, parent_path)
```

`infer_dirs_from_files`: для parent `/Books/Math` найти distinct next segment
из rows где `parent_path = '/Books/Math'` (files) или `parent_path LIKE '/Books/Math/%'`.

### Path normalization layer

Единая функция `cloud_parent_key(path) -> str` для query:

- принимает `/Books/Math` или `Books/Math`
- ищет в БД оба варианта (`parent_path IN (?, ?)`)
- документировать как tech debt; long-term — normalize on write in CloudScanner

### Snapshot selection for tree

Новый helper `select_snapshot_for_tree(root_path)`:

1. Если есть partial scan covering `root_path` or ancestor → use composite with
   that update
2. Else if base scan has children at root_path → use composite base
3. Else → **warning** `cloud_snapshot.freshness_warning = "no_scan_covering_path"`
   + optional `--allow-empty` vs hard error

Не полагаться на fallback «last cloud scan» без проверки coverage.

---

## Text header (Termux UX)

```
sync_tree:v2  /Books/Math  depth=3
policy: Books/Math/База[B] DAO[B] pro/agents/agents_week[B] ...
snapshot: scan #2426 (2026-08-08)  local: #2420
legend: [B] bisync  [D] download  [L] local-only  [.] cloud  [P] parent-of-synced
```

Предупреждения (если есть):

```
WARN: cloud snapshot older than 2 days for /Books — run: ydm-scan-cloud /Books
WARN: policy/filter hash mismatch — run: sync_policy render-filters --apply
```

---

## CLI changes

### New / changed flags

```text
--policy-path PATH          default: var/sync_policy.json
--use-policy                default: True when --backend rclone
--schema {v1,v2}            default: v2 for text, v1 available for compat
--no-local-scan             default: True in ydm-tree alias (change!)
--infer-dirs-from-files     default: True
--snapshot-min-coverage PATH  fail or warn if no scan covers path
```

### Deprecated (keep working)

```text
--filter-path               override; if omitted and --use-policy, derive from policy
--backend api               unchanged exclude-dirs path
```

### Alias changes (`~/.bashrc`)

```bash
YDM_POLICY="/root/notes/pro/ydm/var/sync_policy.json"
YDM_BISYNC_FILTER="$YDM_LOCAL_ROOT.bisync.filters"

ydm-tree() {
  python3 "$YDM_DIR/tools/sync_tree.py" \
    --db-path "$YDM_DB" --backend rclone \
    --local-root "$YDM_LOCAL_ROOT" \
    --policy-path "$YDM_POLICY" \
    --schema v2 --format text --text-tree \
    --no-local-scan
}

ydm-tree-path() {
  python3 "$YDM_DIR/tools/sync_tree.py" ... \
    --path "$1" --depth "${2:-3}" \
    --policy-path "$YDM_POLICY" \
    --schema v2 --format text --text-tree \
    --no-local-scan
}
```

Default depth для `ydm-tree-path`: **3** (было 2 — недостаточно для
`Books/Math/База`).

---

## Module structure

```text
tools/sync_tree.py           CLI entry, render, argparse
tools/sync_tree_policy.py    NEW: policy overlay, markers, orphan detect
tools/sync_tree_cloud.py     NEW: child discovery, path normalize, snapshot pick
```

Keep new modules small; avoid circular imports with `sync_policy.py` — import
only pure helpers (`load_policy`, `policy_paths_by_mode`, `normalize_entry`).

---

## JSON contract stability

| Schema | Status | Consumers |
|--------|--------|-----------|
| `sync_tree:v1` | frozen | existing scripts, CI smoke |
| `sync_tree:v2` | new default | ydm-tree, AI agents |

Migration: v2 payload includes `"v1_compat"` optional block mapping old
`sync_status` for tools that haven't upgraded.

---

## Integration with existing commands

| Command | Relationship |
|---------|--------------|
| `ydm-sync-state` | tree header reuses same policy/bisync status payload |
| `ydm-sync-add/rm` | no change; after add, tree reflects new policy |
| `sync_policy inspect` | tree may show `risk:` hint on `[D]` nodes with reason |
| `report diff` | independent; tree doesn't replace diff |

---

## Performance

- Default `--no-local-scan` for aliases (local scans every ~30 min from bisync job)
- Cache composite snapshot (already in Analyzer, 5 min TTL)
- Optional `--local-scan` for on-demand accuracy before diff/debug
- `sync_percent` calculation: skip for subtrees with `cloud_count=0`

---

## Testing strategy

1. **Unit:** marker logic, policy longest-prefix, infer_dirs_from_files (synthetic DB)
2. **Fixture DB:** scan #1 + partial #866 + policy json → expected tree JSON
3. **Shell:** `tests/test_sync_tree.sh` — `--help`, v2 text output contains legend
4. **Manual:** after scan #2426 completes, `ydm-tree-path /Books/Math 3`

---

## Open questions

1. **Collapse default:** оставить `--collapse-synced` или для v2 default `--show-all`
   under synced subtrees only? → *Proposal:* default collapse, но всегда показывать
   policy paths даже если cloud children empty.

2. **Disabled mode in tree:** показывать `[X]` или скрывать? → *Proposal:* показывать
   если entry exists in policy.

3. **Full disk root `/`:** слишком тяжело без full scan → *Proposal:* warn + suggest
   `ydm-tree-path /Books 2`.
