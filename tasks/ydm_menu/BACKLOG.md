# YDM Menu — Backlog

*Дата:* 2026-08-08
*Scope:* полная реализация без урезания MVP; agent CLI сохраняется.

---

## Phase 0 — Documentation ✅

| ID | Task | Status |
|----|------|--------|
| 0.1 | GAP.md | ✅ |
| 0.2 | DESIGN.md | ✅ |
| 0.3 | BACKLOG.md | ✅ |
| 0.4 | Link from tasks/sync_manager, README | ✅ |

**Exit:** docs readable standalone.

---

## Phase 1 — Foundation & status bar

*Estimate:* 4–6 h

| ID | Task | Files | Acceptance |
|----|------|-------|------------|
| 1.1 | `ydm_menu_config.py` — paths, env `YDM_*`, defaults | new | unit test defaults |
| 1.2 | `ydm_menu_status.py` — `MenuStatus`, load from policy+bisync | new | matches `ydm-sync-state` facts |
| 1.3 | `ydm_menu_screens.py` — header, main menu render | new | ≤72 cols |
| 1.4 | `ydm_menu_prompts.py` — int, ints, yes/no, confirm | new | tests for parsing |
| 1.5 | `ydm_menu.py` — REPL skeleton, q/0 back, loop | new | `--help` works |
| 1.6 | Alias `ydm` / `ydm-menu` in bashrc | `~/.bashrc` | `ydm` shows menu |

**Exit:** menu opens, shows live status, quit works; items 1–9 stub «not implemented».

---

## Phase 2 — Policy actions library + fix remove

*Estimate:* 6–8 h

| ID | Task | Files | Acceptance |
|----|------|-------|------------|
| 2.1 | `ydm_menu_actions.py`: `action_add(path, mode)` | new | policy + render-filters |
| 2.2 | `action_remove(path, delete_local)` policy-first | new | policy + filters + optional delete |
| 2.3 | `offer_resync_if_needed()` | new | replaces bash heredoc |
| 2.4 | Fix `ydm-sync-rm` → policy-first (backward compat) | bash + maybe sync_policy wrapper | rm updates policy |
| 2.5 | Risk dialog helper `handle_add_blocked(inspect)` | new | download/scan/force/cancel |
| 2.6 | Unit tests for add/remove with temp policy | tests | no live cloud |

**Exit:** actions callable from Python tests; `ydm-sync-rm` consistent with policy.

---

## Phase 3 — Orphan discovery

*Estimate:* 4–6 h

| ID | Task | Files | Acceptance |
|----|------|-------|------------|
| 3.1 | `ydm_menu_orphans.py` — `list_orphan_paths()` | new | finds АнГем on real mirror |
| 3.2 | Reuse sync_tree build + policy overlay (import, not subprocess) | orphans | same as `[L]` in tree |
| 3.3 | JSON export `--non-interactive orphans --format json` | ydm_menu.py | schema `ydm_menu_orphans:v1` |
| 3.4 | `tests/test_ydm_menu_orphans.py` | tests | synthetic FS + DB |

**Exit:** `python3 tools/ydm_menu.py orphans --format json` lists orphans.

---

## Phase 4 — Menu screens (full)

*Estimate:* 8–12 h

| ID | Task | Menu # | Acceptance |
|----|------|--------|------------|
| 4.1 | Show sync tree (+ pager, path/depth pick) | 1 | like `ydm-tree 4` |
| 4.2 | Add from cloud (rclone lsf pick) | 2 | like `ydm-sync-pick` |
| 4.3 | Add local orphans (multi-select, mode) | 3 | АнГем without typing path |
| 4.4 | Remove from sync (confirm, delete optional) | 4 | policy-first |
| 4.5 | Run bisync | 5 | lock check, log tail |
| 4.6 | Resync baseline | 6 | dry-run → confirm → apply |
| 4.7 | Cloud scan submenu | 7 | spawn scan with path pick |
| 4.8 | Detailed status | 8 | human text |
| 4.9 | Short help | 9 | 15 lines max |

**Exit:** all menu items functional end-to-end on device.

---

## Phase 5 — Polish & integration

*Estimate:* 4–6 h

| ID | Task | Acceptance |
|----|------|------------|
| 5.1 | Refactor `ydm-sync-add` to call shared `action_add` OR document duplicate | no drift |
| 5.2 | Update `ydm-help` — «start with `ydm`» | help text |
| 5.3 | README.ru / README.md paragraph | human vs agent |
| 5.4 | `tasks/ydm_menu/HOW_TO_USE.md` — 1-page human guide | Russian |
| 5.5 | Batch orphan add → single resync prompt | UX |
| 5.6 | Error screens: no orphans, no cloud scan, lock busy | friendly messages |

---

## Phase 6 — Tests & CI

*Estimate:* 4–6 h

| ID | Task | Acceptance |
|----|------|------------|
| 6.1 | `tests/test_ydm_menu.py` — prompts, status, REPL smoke | unittest |
| 6.2 | `--non-interactive` scripted session test | stdin fixture |
| 6.3 | CI: `ydm_menu.py --help`, orphans JSON | `.github/workflows/ci.yml` |
| 6.4 | Manual checklist in BACKLOG (device) | signed off |

---

## Phase 7 — Optional script mode (agent bridge)

*Estimate:* 2–3 h

| ID | Task | Acceptance |
|----|------|------------|
| 7.1 | `ydm_menu.py add-orphan --path … --mode … --apply` | agent can call |
| 7.2 | JSON stdout for all script subcommands | machine-readable |

Not a substitute for `sync_policy.py`; convenience wrapper only.

---

## Manual verification checklist

> **Не выполнять как написано (2026-08-23).** Пункты опираются на
> `Books/Math/База` как двунаправленную и `Books/Math/АнГем` как сироту —
> ни того, ни другого в политике нет с 14.08, там сплошной `Books: disabled`.
> Хуже того, воссоздать их на боевом демоне означает убрать `Books` из
> `exclude-dirs` и **начать реальную синхронизацию** `/Books` — операцию из
> инцидента 14.08.
>
> Заменено 2026-08-23 автоматическими проверками на стенде —
> [`tests/test_sync_bench.py`](../../tests/test_sync_bench.py),
> [`tasks/sync_bench/`](../sync_bench/README.md).

### Что было ручным пунктом и чем стало

| Пункт | Где проверяется теперь |
|---|---|
| шапка `ydm` согласована с фактами | `TestMenuHeader.test_header_counts_match_the_policy_file` — списки режимов в шапке сверяются с файлом политики |
| шапка не показывает чужого | `test_header_lists_the_bench_policy_not_the_real_one` |
| Menu 3: список включает сироту | `test_orphans_finds_the_prepared_orphans_and_only_those` — сверяется **точное множество**, не вхождение |
| список сирот согласован с деревом | `test_the_orphan_list_agrees_with_the_tree` — меню и дерево выводят `[L]` разными путями; расхождение означает, что меню предлагает добавить то, чего дерево не показывает |
| Menu 3: добавленное рисуется `[B]` | `TestTreeMarkers` — все девять маркеров, обе семантики |
| Menu 4: remove правит политику | `TestPolicyEditingStaysOnTheBench` — правится файл стенда, боевой сверяется побайтно до и после |
| правка видна в рендере | `test_removing_a_path_changes_its_marker` — `[B~]` → `[L]` |
| Menu 8 не падает | `test_the_menu_renders_when_the_backend_is_unavailable` |
| шапка демона не выдумывает bisync | `test_the_daemon_header_does_not_invent_bisync_facts` |

### Осталось ручным

| Пункт | Почему |
|---|---|
| Menu 5: bisync уважает lock | Реальный процесс и реальный обмен данными — стенд отвечает на «что система *говорит*», не «что делает с файлами» |
| Resync prompt при необходимости | То же: осмысленно только на живом bisync |
| `ydm-menu` через rclone на Android | Нужна вторая машина; [`tasks/sync_unification/BACKLOG.md`](../sync_unification/BACKLOG.md) |
| `ydm-sync-add /path`, agent JSON tools | Покрыты своими тестами (`test_ydm_menu.py`, `test_sync_backends.py`); отдельным ручным пунктом быть перестали |

---

## Risk register

| Risk | Mitigation |
|------|------------|
| REPL hard to test | extract prompts/actions; `--non-interactive` |
| Remove data loss | double confirm + type name |
| Policy/filter drift | always render-filters after policy mutate |
| Slow tree build on menu open | cache orphans 60s TTL in menu session |
| `ydm` name collision | document; expert uses `ydm-sync-*` |
| Cyrillic in rclone lsf | already solved in sync-pick; reuse |

---

## Dependency graph

```text
Phase 0 → Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5 → Phase 6 → Phase 7
              │         │
              │         └── fix ydm-sync-rm (can ship early)
              └── blocks nothing
```

**Parallelizable:** Phase 3 orphans can start after 1.2; Phase 2.4 remove fix independent.

---

## Total estimate

| Phase | Hours |
|-------|-------|
| 0 | done |
| 1 | 4–6 |
| 2 | 6–8 |
| 3 | 4–6 |
| 4 | 8–12 |
| 5 | 4–6 |
| 6 | 4–6 |
| 7 | 2–3 |
| **Sum** | **~32–47 h** |

---

## Current state

| Item | Status |
|------|--------|
| sync_tree v2 | ✅ (orphan markers) |
| sync_policy | ✅ |
| User request | ✅ docs this task |
| Implementation | ✅ Phases 1–4, 6 (menu REPL, orphans, actions, tests, CI) |
| Phase 5 polish | partial (`ydm-help`, HOW_TO_USE; `ydm-sync-add` still bash) |
| Phase 7 script mode | partial (`orphans` subcommand only) |
