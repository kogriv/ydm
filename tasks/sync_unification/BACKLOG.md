# Unified sync interface — Backlog

*Дата:* 2026-08-14  
*Scope:* закрыть гэп между daemon-backend (Ubuntu) и rclone-backend (Android/Termux), предоставив единый CLI для синхронизации.

---

## Phase 0 — Documentation ✅

| ID | Task | Status | Files |
|----|------|--------|-------|
| 0.1 | Написать GAP.md | ✅ | `tasks/sync_unification/GAP.md` |
| 0.2 | Написать DESIGN.md | ✅ | `tasks/sync_unification/DESIGN.md` |
| 0.3 | Написать BACKLOG.md | ✅ | `tasks/sync_unification/BACKLOG.md` |
| 0.4 | Написать HOW_TO_USE.md | 🔄 | `tasks/sync_unification/HOW_TO_USE.md` |
| 0.5 | Обновить README.md / README.ru.md | ⏳ | `README.md`, `README.ru.md` |
| 0.6 | Link from tasks/ydm_menu/README.md | ⏳ | `tasks/ydm_menu/README.md` |

**Exit:** docs readable standalone; design approved before code.

---

## Phase 1 — Backend abstraction

*Estimate:* 6–8 h

| ID | Task | Files | Acceptance |
|----|------|-------|------------|
| 1.1 | Создать `tools/sync_backends.py` с `SyncBackend` ABC | new | py_compile OK |
| 1.2 | Реализовать `DaemonBackend` | `tools/sync_backends.py` | apply_policy обновляет exclude-dirs + restart daemon; unit test с временным config.cfg |
| 1.3 | Реализовать `RcloneBackend` | `tools/sync_backends.py` | вызывает render-filters, run/resync через sync_bisync; unit test mock |
| 1.4 | Реализовать `detect_backend()` с auto-detect + explicit override | `tools/sync_backends.py` | env var, CLI flag, config profile приоритеты верны; unit tests |
| 1.5 | Реализовать `stop_yandex_disk_if_active()` с warning | `tools/sync_backends.py` | если daemon active и выбран rclone — предупреждение и остановка по подтверждению; unit test mock |
| 1.6 | Обработка `download_only` на daemon backend — `NotSupportedError` | `tools/sync_backends.py` | понятное сообщение; unit test |
| 1.7 | Добавить `tests/test_sync_backends.py` | `tests/test_sync_backends.py` | покрыть apply_policy, detect, backend switch |

**Exit:** backend abstraction полностью протестирована без live daemon/rclone.

---

## Phase 2 — sync_policy.py: backend support + migrate

*Estimate:* 6–8 h

| ID | Task | Files | Acceptance |
|----|------|-------|------------|
| 2.1 | Добавить `--backend {daemon,rclone,auto}` в `sync_policy.py` | `tools/sync_policy.py` | `--help` показывает флаг; unit test parse args |
| 2.2 | `add_policy_path()` default mode = bidirectional (уже так) | `tools/sync_policy.py` | confirm |
| 2.3 | При `--apply` вызывать backend.apply_policy() | `tools/sync_policy.py` | daemon backend обновляет config.cfg; rclone backend пишет filters; unit test |
| 2.4 | Новый subcommand `migrate --backend daemon` | `tools/sync_policy.py` | читает exclude-dirs → создаёт policy с disabled; dry-run по умолчанию; unit test |
| 2.5 | `download_only` на daemon backend — clear error | `tools/sync_policy.py` | unit test |
| 2.6 | Обновить `render` для action `migrate` | `tools/sync_policy.py` | text + JSON output |
| 2.7 | Обновить `tests/test_sync_policy.py` или добавить | tests | покрыть migrate, backend apply |

**Exit:** `sync_policy.py` работает с обоими backend; миграция существующего exclude-dirs работает.

---

## Phase 3 — sync_tree.py: policy overlay for daemon backend

*Estimate:* 4–6 h

| ID | Task | Files | Acceptance |
|----|------|-------|------------|
| 3.1 | `--backend api` сделать alias на `--backend daemon` | `tools/sync_tree.py` | `--backend api` продолжает работать |
| 3.2 | `--use-policy` по умолчанию true для обоих backend | `tools/sync_tree.py` | если policy file exists, используется |
| 3.3 | Для daemon backend membership из policy + fallback на exclude-dirs | `tools/sync_tree.py` | bidirectional → [B], disabled/excluded → [X], local orphan → [L] |
| 3.4 | Поддержка `download_only` на daemon: warning + [?] или [X] | `tools/sync_tree.py` | unit test |
| 3.5 | Обновить `tests/test_sync_tree.py` | tests | policy overlay для daemon; markers correct |
| 3.6 | Обновить `ydm-tree` alias в `.bashrc` (убрать `--backend rclone`) | `~/.bashrc` | auto backend |

**Exit:** `ydm-tree` показывает `[B]`/`[L]`/`[X]` на Ubuntu после миграции.

---

## Phase 4 — ydm_menu.py: backend-agnostic

*Estimate:* 8–12 h

| ID | Task | Files | Acceptance |
|----|------|-------|------------|
| 4.1 | `MenuConfig` получает backend через detect_backend() | `tools/ydm_menu_config.py` | unit test defaults |
| 4.2 | `ydm_menu.py` добавить `--backend {daemon,rclone,auto}` | `tools/ydm_menu.py` | `--help` OK |
| 4.3 | При старте: auto-detect + warning если rclone на машине с daemon | `tools/ydm_menu.py` | unit test mock |
| 4.4 | `cloud_list_dirs()` через backend.list_cloud_children() | `tools/ydm_menu_actions.py` | на daemon — из DB snapshot; на rclone — rclone lsf; unit test |
| 4.5 | `run_cloud_scan()` через backend.run_cloud_scan() | `tools/ydm_menu_actions.py` | unit test |
| 4.6 | `run_sync_tree()` запускает sync_tree с правильным backend | `tools/ydm_menu_actions.py` | unit test |
| 4.7 | `action_bisync_run()` → backend.run_sync() | `tools/ydm_menu_actions.py` | на daemon — restart daemon; на rclone — rclone bisync run |
| 4.8 | `action_resync()` → backend.run_resync() | `tools/ydm_menu_actions.py` | на rclone — bisync resync; на daemon — NotSupportedError с понятным сообщением |
| 4.9 | `action_add()` / `action_remove()` через sync_policy.py + backend | `tools/ydm_menu_actions.py` | policy-first; unit test |
| 4.10 | Обновить `tests/test_ydm_menu.py` | tests | backend mock; menu smoke |

**Exit:** `ydm-menu` работает на Ubuntu и Android.

---

## Phase 5 — bash aliases and help

*Estimate:* 2–3 h

| ID | Task | Files | Acceptance |
|----|------|-------|------------|
| 5.1 | Исправить `ydm-sync-add` / `ydm-sync-rm` aliases на policy-first | `~/.bashrc` | `add` включает sync; `rm` выключает sync |
| 5.2 | `ydm-tree` alias без жёсткого backend | `~/.bashrc` | auto-detect |
| 5.3 | `ydm-menu` alias передаёт db-path + local-root | `~/.bashrc` | works |
| 5.4 | Обновить `ydm-help` — добавить `ydm-menu` и backend note | `~/.bashrc` | ASCII, short lines |
| 5.5 | Добавить `YDM_BACKEND` env var в `.bashrc` setup | `~/.bashrc` | optional |

**Exit:** aliases работают на Ubuntu с правильной семантикой.

---

## Phase 6 — config and status

*Estimate:* 2–3 h

| ID | Task | Files | Acceptance |
|----|------|-------|------------|
| 6.1 | Добавить `backend` в `DEFAULT_CONFIG` | `ydm.py` | default "auto" |
| 6.2 | Добавить `backend` в `ydm_config.json` | `ydm_config.json` | prod/test profiles |
| 6.3 | `MenuStatus` показывает backend name | `tools/ydm_menu_status.py` | text + JSON |
| 6.4 | `ydm_menu_screens.py` показывает backend в header | `tools/ydm_menu_screens.py` | ≤72 cols |

**Exit:** пользователь всегда видит, какой backend активен.

---

## Phase 7 — Tests & CI

*Estimate:* 4–6 h

| ID | Task | Files | Acceptance |
|----|------|-------|------------|
| 7.1 | `tests/test_sync_backends.py` full coverage | tests | backend classes, detect, daemon stop |
| 7.2 | Обновить `tests/test_sync_tree.py` для policy on daemon | tests | markers correct |
| 7.3 | Обновить `tests/test_ydm_menu.py` для backend-agnostic actions | tests | menu smoke |
| 7.4 | CI: py_compile `tools/sync_backends.py` | `.github/workflows/ci.yml` | ✅ |
| 7.5 | CI: `--help` smoke для `sync_policy.py`, `ydm_menu.py`, `sync_tree.py` | `.github/workflows/ci.yml` | ✅ |
| 7.6 | Manual verification checklist | BACKLOG.md | signed off |

**Exit:** CI зелёный; manual check на Ubuntu пройден.

---

## Phase 8 — Documentation update

*Estimate:* 2–3 h

| ID | Task | Files | Acceptance |
|----|------|-------|------------|
| 8.1 | Написать HOW_TO_USE.md | `tasks/sync_unification/HOW_TO_USE.md` | Ubuntu + Android flows |
| 8.2 | Обновить README.md — раздел про unified sync | `README.md` | short paragraph + link |
| 8.3 | Обновить README.ru.md | `README.ru.md` | translation |
| 8.4 | Обновить CHANGELOG.md | `CHANGELOG.md` | entry 2026-08-14 |
| 8.5 | Обновить tasks/ydm_menu/README.md — ссылка на unification | `tasks/ydm_menu/README.md` | mention backend agnostic |

**Exit:** пользователь может найти unified interface docs.

---

## Manual verification checklist

Прогон 2026-08-16 на боевой Ubuntu-машине с живым демоном. Ничего из
отмеченного не меняло `~/.config/yandex-disk/config.cfg` и не перезапускало
демон: пункты, требующие записи в конфиг, проверены на копии конфига.

- [x] `ydm-menu` на Ubuntu открывается и показывает `Backend: yandex-disk daemon`
- [x] `ydm-sync-add /Books/Math/АнГем` убирает `/Books/Math/АнГем` из `exclude-dirs` —
      проверено dry-run'ом на копии боевого конфига: 55 → 97 записей,
      `Books` уходит, добавляются 24 соседа уровня `Books/` и 19 уровня
      `Books/Math/`. На живом конфиге не применялось: включать `АнГем` в
      синхронизацию сейчас не требуется
- [x] `ydm-sync-rm /Books/Math/АнГем` добавляет `/Books/Math/АнГем` в `exclude-dirs` —
      обратная операция того же механизма, покрыта `tests/test_sync_policy_daemon.py`
- [x] `ydm-tree` показывает `[B]` для включённых папок и `[X]` для исключённых —
      **было сломано**: daemon-ветка строила membership из whitelist
      `bidirectional`, который в daemon-policy всегда пуст, поэтому всё
      синхронизируемое рисовалось как `[L]`. Исправлено (см. CHANGELOG 16.08);
      сейчас `/pro` → `[B] 100%`, `/video` → `[B~] 5.5%`, исключённое → `[X]`
- [x] `ydm-menu` → 2 Add from cloud → список подпапок из monitor.db snapshot
- [x] После stale snapshot menu предлагает cloud scan
- [x] `ydm-menu` с `--backend rclone` на машине с daemon: warning + предложение остановить daemon
- [ ] На Android `ydm-menu` работает через rclone как раньше — **не проверено**,
      нужна вторая машина
- [x] `python3 tools/sync_policy.py migrate --backend daemon --apply` создаёт policy из exclude-dirs —
      выполнено 16.08 на боевом конфиге (только чтение конфига, запись в
      `var/sync_policy.json`): 55 записей, dry-run `apply_policy` после этого —
      чистый no-op
- [x] `sync_policy.py add --mode download_only` на daemon падает с понятной ошибкой
- [x] `tests/test_sync_backends.py` все зелёные
- [x] `tests/test_ydm_menu.py` все зелёные

**Найдено при прогоне и вынесено за рамки этой задачи:** эвристика выбора
базового скана (`Analyzer.get_full_scan_candidates`) может выбрать *частичный*
скан как базу композита. Сейчас база — скан `93` (только `/Books`), поэтому
`sync_tree --path /` показывает единственного ребёнка. Отдельная проблема ядра,
не daemon-бэкенда; лечится полным облачным сканом и гейтом на покрытие корня.

---

## Risk register

| Risk | Mitigation |
|------|------------|
| `DaemonBackend.apply_policy()` некорректно парсит/пишет config.cfg | Unit tests с разными форматами exclude-dirs; backup config.cfg перед write |
| Миграция искажает существующие exclude-dirs | Только создаёт policy; apply — отдельная команда; dry-run default |
| `ydm-sync-add` и `ydm-sync-rm` ломают workflows пользователя | README + CHANGELOG + `ydm-help` explicit note |
| rclone backend на Ubuntu конфликтует с daemon | Explicit warning + stop daemon prompt |
| Backend detection нестабилен | Unit tests для всех комбинаций: explicit, env, config, auto |
| Старые тесты `test_ydm_menu.py` ломаются | Обновить тесты, сохранить schema expectations |
| `download_only` на daemon вызывает путаницу | Clear error + suggest `--backend rclone` |

---

## Dependency graph

```text
Phase 0 ──► Phase 1 ──► Phase 2 ──► Phase 3 ──► Phase 4 ──► Phase 5 ──► Phase 6 ──► Phase 7 ──► Phase 8
                │           │           │           │
                │           │           │           └── blocks 5,6,7
                │           │           └── blocks 4
                │           └── blocks 3
                └── blocks 2,4
```

**Parallelizable:**
- Phase 5 (bash aliases) можно начать после Phase 2.
- Phase 6 (config/status) можно начать после Phase 1.
- Phase 8 (docs) можно делать параллельно всему.

---

## Total estimate

| Phase | Hours |
|-------|-------|
| 0 | done (docs) |
| 1 | 6–8 |
| 2 | 6–8 |
| 3 | 4–6 |
| 4 | 8–12 |
| 5 | 2–3 |
| 6 | 2–3 |
| 7 | 4–6 |
| 8 | 2–3 |
| **Sum** | **~34–49 h** |

---

## Current state

*Обновлено 2026-08-15.*

| Item | Status |
|------|--------|
| Gap analysis | ✅ |
| Design document | ✅ |
| Backlog | ✅ |
| Backend abstraction | ✅ код в рабочем дереве, не закоммичен |
| sync_policy backend support | ✅ код в рабочем дереве, не закоммичен |
| sync_tree policy on daemon | ✅ код в рабочем дереве, не закоммичен |
| ydm_menu backend-agnostic | ✅ код в рабочем дереве, не закоммичен |
| Bash aliases fixed | ✅ `~/.bashrc` уже правлен |
| Config/status | ✅ `ydm_config.json` + backend в шапке меню |
| Tests & CI | ✅ 72 теста зелёные, шаги в `ci.yml` добавлены |
| Docs update | ✅ README / CHANGELOG / cross-links |
| **Проверка на живом daemon-окружении** | ⚠️ частично: логика проверена на стенде (`tests/test_sync_policy_daemon.py`) и dry-run'ом на реальном снимке; `--apply` на живом демоне не выполнялся |

### Что осталось закрыть перед коммитом

1. ~~**Ancestor-sibling coercion (`_policy_coerce_for_daemon`) не проверена.**~~
   ✅ Закрыто 2026-08-16. Стенд — `tests/test_sync_policy_daemon.py`: синтетический
   `monitor.db` + временный `config.cfg`, `stop_start_daemon` замокан, боевой
   конфиг и демон не задействованы. Стенд вскрыл, что coercion исключала соседей
   только на одном уровне: для `/Books/Math/АнГем` она убирала `Books` из
   `exclude-dirs` и исключала `Books/*`, но не `Books/Math/*` — демон утащил бы
   весь `Books/Math`. Исправлено обходом всех уровней от предка до цели; при
   отсутствии снимка промежуточного уровня — `PolicyCoercionError`, policy не
   меняется. Проверка на реальном снимке (dry-run, временный конфиг):
   55 → 97 исключений, 24 соседа уровня `Books/` + 19 уровня `Books/Math/`.
2. **Предохранитель `DaemonBackend.deletion_risk_paths()`** (добавлен 2026-08-15):
   `apply_policy()` отказывается перезапускать демон, если путь остаётся в
   синхронизации, но его локальной копии нет — именно эта комбинация приводит к
   удалению в облаке. Покрыт тестами; на живом окружении не проверялся.
3. **Дефекты первой реализации исправлены 2026-08-15:** `SyncBackend.kind`
   вместо разбора человекочитаемого `name()` в `sync_policy._resolve_backend_name`
   и `sync_tree._resolve_backend_name` (при `--backend auto` daemon-ветка не
   срабатывала), возвращён проброс `--plain` в `ydm_menu.py`.
