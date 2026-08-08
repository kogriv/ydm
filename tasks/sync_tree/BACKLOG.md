# Sync Tree v2 — Backlog

*Дата:* 2026-08-08
*Prerequisite:* cloud scan `/Books/Math` (scan #2426, in progress)

Этапы упорядочены по value/risk. Каждый этап — отдельный коммит/PR.

---

## Phase 0 — Documentation ✅

| ID | Task | Status |
|----|------|--------|
| 0.1 | Gap analysis | ✅ `GAP.md` |
| 0.2 | Design doc | ✅ `DESIGN.md` |
| 0.3 | Backlog | ✅ this file |
| 0.4 | Link from `tasks/sync_manager/README.md` | ✅ |

## Phase 1 — Quick fixes ✅

| ID | Task | Status |
|----|------|--------|
| 1.1 | Fix `ydm-tree` aliases (policy, v2, no-local-scan, depth 3) | ✅ `~/.bashrc` |
| 1.2 | `YDM_POLICY`, `_ydm_require_env` fail-fast | ✅ |
| 1.3 | Snapshot coverage warning in header | ✅ |
| 1.4 | README workaround | ✅ |

## Phase 2 — Policy overlay ✅

| ID | Task | Status |
|----|------|--------|
| 2.1 | `tools/sync_tree_policy.py` | ✅ |
| 2.2 | `--policy-path`, `--schema v2` | ✅ |
| 2.3 | TreeNode v2 fields | ✅ |
| 2.4 | Text renderer `[B]`/`[D]`/`[L]`/`[.]`/`[P]` + legend | ✅ |
| 2.5 | Orphan detection | ✅ |
| 2.6 | Policy summary in header | ✅ |

## Phase 3 — Cloud tree robustness ✅

| ID | Task | Status |
|----|------|--------|
| 3.1 | `infer_dirs_from_files()` | ✅ |
| 3.2 | Dual `parent_path` lookup in counts | ✅ |
| 3.3 | `select_snapshot_for_tree()` | ✅ |
| 3.4 | Analyzer fallback improvement | deferred |

## Phase 4 — UX polish (partial) ✅

| ID | Task | Status |
|----|------|--------|
| 4.1 | Filter mismatch warning | ✅ |
| 4.2 | `ydm-help` legend update | ✅ |
| 4.3 | README link | ✅ |
| 4.4 | Collapse keeps local orphans | ✅ |

## Phase 5 — Tests & CI ✅

| ID | Task | Status |
|----|------|--------|
| 5.1 | `tests/test_sync_tree.py` | ✅ |
| 5.2 | CI step | ✅ |

---

## Deferred / future

| ID | Idea | Notes |
|----|------|-------|
| F.1 | `ydm-tree --json` alias for agents | thin wrapper |
| F.2 | Merge `ydm-sync-state` summary into tree footer | avoid duplication |
| F.3 | Color markers (if terminal supports) | Termux limited |
| F.4 | `sync_tree` TUI picker like `ydm-sync-pick` | separate task |
| F.5 | Normalize `parent_path` on write in CloudScanner | migration + dedupe |

---

## Risk register

| Risk | Mitigation |
|------|------------|
| Breaking v1 JSON consumers | keep `--schema v1`; v2 opt-in until Phase 2 complete |
| Slow tree on `/` with full scan | warn; default path `/Books` in docs |
| False orphan if local scan stale | show `local_scan_id` age in header |
| Cyrillic path mismatch (`АнГем` vs `Ангем`) | normalize NFC; document exact cloud names |
| Editing `~/.bashrc` outside repo | document in CONTRIBUTING; optional install script |

---

## Verification checklist (final)

After Phases 1–2 minimum:

- [ ] `ydm-tree-path /Books/Math 3` — non-empty tree
- [ ] `Books/Math/База` marked `[B]` (bidirectional)
- [ ] `Books/Math/АнГем` marked `[L]` (local orphan) if not in policy
- [ ] `ydm-sync-state` bidirectional list matches tree `[B]` nodes
- [ ] Stale snapshot → WARN in header, not silent empty tree
- [ ] `python3 tools/sync_tree.py --schema v1 ...` still works (compat)

---

## Current scan status

| Scan ID | Path | Status | Purpose |
|---------|------|--------|---------|
| 2426 | `/Books/Math` | started 2026-08-08 | unblock Phase 1 manual test |

After completion:

```bash
sqlite3 monitor.db "SELECT COUNT(*) FROM files WHERE scan_id=2426 AND type='dir';"
# Expect: >0 dir rows under Books/Math
```
