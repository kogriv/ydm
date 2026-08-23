# Sync Tree v2 — policy-aware дерево синхронизации

*Статус:* **Implemented** (v2 policy-aware tree; see Phase 1–5 in BACKLOG.md)
*Контекст:* восстановление и развитие `ydm-tree` / `tools/sync_tree.py` после
введения policy-aware bisync (`var/sync_policy.json`, `*.bisync.filters`).

## Зачем отдельная задача

`tools/sync_tree.py` написан как переходный инструмент для `exclude-dirs`
(`--backend api`) и позже получил whitelist-режим (`--backend rclone`), но
**не был обновлён** под policy layer (`tools/sync_policy.py`), который уже
разделяет:

- bidirectional (scheduled `rclone bisync`)
- download_only (локальная копия без bisync)
- disabled (в policy, но не материализовано)

В результате `ydm-tree` перестал быть надёжным primary UI для ответа на
вопрос «что и как синкается».

## Документация

| Документ | Содержание |
|----------|------------|
| [GAP.md](./GAP.md) | Текущие проблемы, симптомы, root cause |
| [DESIGN.md](./DESIGN.md) | Целевая модель, schema v2, UX, интеграции |
| [BACKLOG.md](./BACKLOG.md) | Этапы работ, acceptance criteria, риски |

## Связанные документы

- [`tasks/sync_bench/README.md`](../sync_bench/README.md) — проверка маркеров
  дерева на синтетическом стенде: сейчас покрыты 4 значения `display_marker()`
  из 9, сквозного теста рендера нет
- [`tasks/sync_manager/README.md`](../sync_manager/README.md) — исходный sync manager
- [`tasks/rclone_backend/POLICY_AWARE_BISYNC.md`](../rclone_backend/POLICY_AWARE_BISYNC.md) — policy layer (реализован)
- [`tasks/rclone_backend/README.md`](../rclone_backend/README.md) — rclone backend
- [`tasks/sync_unification/README.md`](../sync_unification/README.md) — unified backend-agnostic CLI and daemon backend support for policy overlay

## Быстрый workaround (до реализации v2)

```bash
source ~/.bashrc

# 1. Актуальный облачный снимок (обязательно для непустого дерева)
ydm-scan-cloud /Books/Math

# 2. Дерево с bisync-filter (не legacy .filters)
python3 "$YDM_DIR/tools/sync_tree.py" \
  --db-path "$YDM_DB" --backend rclone \
  --local-root "$YDM_LOCAL_ROOT" \
  --filter-path "$YDM_BISYNC_FILTER" \
  --path /Books/Math --depth 3 \
  --format text --text-tree --show-all --no-local-scan

# 3. Список policy-режимов
ydm-sync-state
```

## Триггер задачи (2026-08-08)

Пользователь запустил `ydm-scan-cloud /Books/Math` для восстановления дерева
Math. После завершения scan #2426 можно проверять Phase 1 quick fixes на
реальных данных.
