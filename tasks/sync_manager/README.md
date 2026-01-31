# Sync Manager - Утилита управления синхронизацией Yandex Disk

Утилита для визуализации и управления синхронизацией папок в Yandex Disk.

## Проблема

Текущее управление синхронизацией через ручное редактирование конфига `~/.config/yandex-disk/config.cfg` неудобно:
- Нет визуализации того, что фактически синхронизируется
- Сложно добавить только одну подпапку (нужно исключить все остальные)
- Нет предупреждений об опасных именах папок (запятые, скобки)

## Решение

Утилита предоставляет:
1. **Дерево синхронизируемых папок** - визуализация текущего состояния
2. **Инспекция папок** - просмотр структуры с предупреждениями
3. **Управление синхронизацией** - добавление/удаление папок с автоматизацией

## Документация

- [TASK_SYNC_MANAGER.md](./TASK_SYNC_MANAGER.md) - полное описание задачи, проблемы и решения
- [simple_sync/](./simple_sync/) - переходные дизайны и быстрые CLI‑тулы

## Быстрые примеры

```bash
# Дерево синхронизации (JSON по умолчанию)
python3 tools/sync_tree.py --path /DAO --depth 2

# Полное дерево с ветками
python3 tools/sync_tree.py --path /DAO --depth 2 --format text --text-tree --show-all

# Управление exclude-dirs (dry-run)
python3 tools/sync_exclude.py add --path /DAO/2

# Применить изменения
python3 tools/sync_exclude.py add --path /DAO/2 --apply

# Текстовый вывод без заголовка
python3 tools/sync_tree.py --format text --text-tree --no-text-header
python3 tools/sync_exclude.py add --path /DAO/2 --format text --no-text-header

# Отключить рестарт демона и локальный скан
python3 tools/sync_exclude.py add --path /DAO/2 --apply --no-restart-daemon --no-local-scan

# Отключить локальный скан и sync_percent
python3 tools/sync_tree.py --no-local-scan --no-sync-percent
```

## Статус

**Частично реализовано** - доступны переходные CLI‑тулы (sync_tree, sync_exclude) в `tools/`.

