# YDM Menu — интерактивный UI для человека

*Статус:* **Implemented** (Phases 1–4, 6; Phase 5/7 частично)
*Контекст:* поверх policy-aware sync (`sync_policy`, `sync_tree v2`, `sync_bisync`)
нужен **диалоговый режим** для Termux, не заменяющий CLI для агентов.

## Зачем отдельная задача

С июля 2026 весь sync-стек **машиночитаемый и корректный**, но **не человекочитаемый**:

- десятки команд (`ydm-sync-add`, `ydm-tree`, `ydm-bisync-resync`, …)
- кириллица в путях
- неочевидная связь `[L]` → «надо ydm-sync-add»
- resync спрашивается только после add, легко пропустить
- remove через `sync_filters`, add через `sync_policy` — разные entry points

**YDM Menu** — единая точка входа `ydm` / `ydm-menu` с цифровым выбором и
короткими подсказками. Все существующие `ydm-*` команды и JSON tools **остаются**
для агентов, CI и скриптов.

## Документация

| Документ | Содержание |
|----------|------------|
| [GAP.md](./GAP.md) | UX-проблемы, разрывы между tools, симптомы |
| [DESIGN.md](./DESIGN.md) | Архитектура menu, экраны, API reuse, Termux UX |
| [BACKLOG.md](./BACKLOG.md) | Полный план реализации по фазам |
| [HOW_TO_USE.md](./HOW_TO_USE.md) | Краткая инструкция для человека |

## Связанные документы

- [`tasks/sync_tree/README.md`](../sync_tree/README.md) — дерево и метки `[B]`/`[L]`
- [`tasks/rclone_backend/POLICY_AWARE_BISYNC.md`](../rclone_backend/POLICY_AWARE_BISYNC.md) — policy layer
- [`tasks/sync_manager/README.md`](../sync_manager/README.md) — исторический sync manager
- [`tasks/sync_unification/README.md`](../sync_unification/README.md) — unified backend-agnostic CLI (`ydm-menu`, `ydm-sync-add`, `ydm-tree`) for daemon and rclone

## Целевой UX (one-liner)

```text
ydm  →  меню  →  «3) Локальные папки в sync»  →  выбрать АнГем  →  [B]  →  resync? Y
```

Без ввода `/Books/Math/АнГем` и без запоминания flags.

## Non-goals (на старте)

- TUI/curses, мышь, цвета (Termux-совместимость важнее)
- Замена `ydm.py` scan/report
- Web UI

## Entry point

```bash
source ~/.bashrc
ydm          # интерактивное меню (alias → tools/ydm_menu.py)
ydm-menu     # то же
ydm orphans  # JSON: локальные сироты (для скриптов)
# ydm-sync-add, ydm-tree, … — без изменений для агента
```
