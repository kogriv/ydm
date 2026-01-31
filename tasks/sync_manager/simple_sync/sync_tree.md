## Simple Sync Tree: временный дизайн (быстрое решение)

Документ описывает переходный CLI‑тул для вывода дерева синхронизации
по снимку облака из БД и текущему `exclude-dirs`.

### Цели
- Получить дерево папок с пометками `[S]/[P]/[-]` (sync/partial/excluded).
- Использовать существующие компоненты (`StorageManager`, `Analyzer`,
  композитный снимок) без дублирования логики.
- Вывод — машиночитаемый JSON и стабильный text.

### Не‑цели
- Сканировать облако при запуске (работаем только с БД).
- Полный рефакторинг (см. `tasks/sync_manager/refactoring/`).

---

## CLI (проект)
```
sync tree [--path /A/B] [--depth N] [--format json|text] [--db-path monitor.db]
         [--collapse-synced] [--show-all] [--text-tree] [--text-header|--no-text-header]
         [--local-scan|--no-local-scan] [--local-scan-delay-sec N]
         [--sync-percent|--no-sync-percent] [--local-root PATH]
```

### Где реализуется
- **Переходное решение:** отдельный скрипт в `tools/` (`tools/sync_tree.py`).
- **Позже:** перенос в `ydm.py` как подкоманда `sync tree`.

### Общий модуль (для переиспользования)
Общая логика доступа к БД и композитному снимку выносится в
`tools/sync_common.py` и используется в `sync_tree` и `sync_exclude`:
- нормализация пути;
- выбор `scan_id` по композитному снимку;
- получение списка дочерних папок по `parent_path`;
- чтение `exclude-dirs`.

### Поведение
- Без `--path`: дерево от корня.
- `--depth` ограничивает глубину **от выбранного `--path`** (по умолчанию 1–2).
- `--format json` для ИИ/скриптов; `text` для человека.
- `--collapse-synced` (дефолт) — разворачивать только ветки, где есть
  синхронизируемые папки. Ветки, полностью исключённые, не раскрываются.
- `--show-all` — показать полное дерево независимо от `exclude-dirs`
  (полезно для диагностики).
- `--text-tree` — рисует ветки `├──`/`└──` в text‑выводе.
- `--no-text-header` — убрать метаданные в text‑выводе.
- `--local-scan` — запуск локального скана перед построением дерева (по умолчанию).
- `--local-scan-delay-sec` — задержка перед локальным сканом.
- `--sync-percent` — расчёт процента синхронизации (по умолчанию).
- `--local-root` — путь локального диска (по умолчанию `/data/ya_disk`).

---

## Источники данных
- **БД**: `monitor.db` (или `--db-path`).
- **Снимок облака**: композит `Analyzer.build_composite_scan()`.
- **Конфиг**: `~/.config/yandex-disk/config.cfg` → `exclude-dirs`.

---

## Структура данных
### Узел дерева (JSON)
```
{
  "path": "/Books",
  "name": "Books",
  "sync_status": "partial",   // full | partial | excluded
  "sync_percent": 66.7,
  "children": [...],          // если depth позволяет
  "children_count": 12        // если children скрыты
}
```

### JSON‑контракт (верхний уровень)
```
{
  "schema": "sync_tree:v1",
  "root": { ... },
  "root_path": "/DAO",
  "root_depth": 2,
  "root_children_count": 2,
  "root_visible_children_count": 1,
  "config_path": ".../config.cfg",
  "warnings": [],
  "collapsed": true,
  "local_scan_started": true,
  "local_scan_error": null
}
```

### Текстовый формат (пример)
```
[S] /
[P] Books
[P]   math
[S]     linal
[-] video
[-]   tmp
```

---

## Расчёт статуса синхронизации
### Базовая логика
Папка считается синхронизируемой, если:
- ни она сама, ни любой её префикс не входит в `exclude-dirs`.

### Статус узла
- **full**: папка синхронизируется и нет исключённых потомков.
- **partial**: папка исключена или имеет исключённых потомков, но есть
  включённые ветки.
- **excluded**: папка исключена и внутри нет включённых веток.

---

## Получение дерева папок из БД
1) Построить композитный снимок:
   - `composite = Analyzer.build_composite_scan()`
   - `base_scan_id = composite["base_scan_id"]`
   - `folder_updates = composite["folder_updates"]`

2) Для любого пути выбрать scan_id:
   - найти самый длинный префикс в `folder_updates`;
   - если найден — использовать его scan_id;
   - иначе — `base_scan_id`.

3) Запросить дочерние папки:
```sql
SELECT name
FROM files
WHERE scan_id = :scan_id
  AND parent_path = :parent_path
  AND type = 'dir'
ORDER BY name;
```
Нормализация:
- для корня `parent_path = ""`;
- `/A` в БД хранится как `/A` (без хвостового `/`).

---

## Алгоритм построения дерева (упрощённо)
1) Вход: `root_path` (из `--path` или `/`), `max_depth`.
2) Рекурсивно или в BFS:
   - получить детей из БД;
   - вычислить sync‑статус узла;
   - если `depth < max_depth` — продолжить вниз.
3) Если включён режим `--collapse-synced`:
   - раскрывать только те ветки, где есть **хотя бы один** узел со статусом
     `full` или `partial`;
   - узлы со статусом `excluded`, у которых **нет** включённых потомков,
     остаются свернутыми (и могут выводиться одной строкой).
3) Возвращать узлы с `children` или `children_count`.

---

## Как получить «свернутое» дерево
### Идея
Показывать только те пути, в поддереве которых есть синхронизируемые папки.
Это позволяет видеть «живые» ветки и не перегружать вывод.

### Правило раскрытия
**Строгий критерий «синхронизируемая ветка»:**
- Узел считается «синхронизируемой веткой», если выполняется одно из условий:
  - `sync_status == full`; или
  - `sync_status == partial`; или
  - среди его потомков есть хотя бы один узел со статусом `full`/`partial`.

**Правило раскрытия:**
- Узел раскрывается, если он сам «синхронизируемая ветка».
- Узел со статусом `excluded` **и без** включённых потомков не раскрывается.

### Дополнительные поля (по желанию)
- `has_synced_descendants`: bool
- `visible_children_count`: int

---

## Просмотр поддерева конкретной папки
### Требование
Показать дерево **от указанной папки** с разворотом на N уровней
относительно этой папки.

### Реализация
- `--path /A/B` задаёт корень поддерева.
- `--depth N` определяет глубину **от этого корня**.
- Логика `--collapse-synced` применяется внутри выбранного поддерева.

---

## Алгоритм подсчёта `has_synced_descendants` (формально)
Определение: `has_synced_descendants(node)` — истинно, если в поддереве
узла есть хотя бы один потомок со статусом `full` или `partial`.

### Рекурсивная схема (DFS)
```
function has_synced_descendants(node):
    if node.children пусты:
        return false
    for child in node.children:
        if child.sync_status in {full, partial}:
            return true
        if has_synced_descendants(child):
            return true
    return false
```

### Итоговое условие «синхронизируемая ветка»
```
is_synced_branch(node) =
    node.sync_status in {full, partial}
    OR has_synced_descendants(node)
```

Примечание: вычисление лучше делать снизу‑вверх, чтобы избежать повторных
обходов (один DFS с возвратом флага наверх).

---

## Оптимизированный однопроходный DFS
Идея: за один проход по дереву вычислить флаг `has_synced_descendants`
и сразу определить, какие узлы «видимы» в режиме `--collapse-synced`.

### Псевдокод (post‑order)
```
function dfs(node):
    has_synced = (node.sync_status in {full, partial})
    visible_children = []

    for child in node.children:
        child_has_synced = dfs(child)
        if child_has_synced:
            visible_children.append(child)
        has_synced = has_synced OR child_has_synced

    node.has_synced_descendants = (has_synced AND
        node.sync_status == excluded AND
        visible_children is not empty)

    node.visible_children = visible_children   // опционально
    node.visible_children_count = len(visible_children)

    return has_synced
```

### Хранение флага в узле (пример)
```
{
  "path": "/Books",
  "name": "Books",
  "sync_status": "partial",
  "has_synced_descendants": true,
  "visible_children_count": 3,
  "children": [ ... ] // если нужно вернуть только видимые
}
```

### Комментарий
- При `--collapse-synced` возвращать `children = visible_children`.
- При `--show-all` возвращать все `children`, игнорируя `visible_children`.

---

## Связанные документы
- `tasks/smart_diff/TASK_SMART_DIFF.md` — композитный снимок.
- `tasks/sync_manager/TASK_SYNC_MANAGER.md` — полная версия sync‑manager.
- `tasks/sync_manager/simple_sync/README.md` — дизайн модификации конфига.

---

## Статус готовности
**Готово к началу реализации.**
Все ключевые решения и интерфейсы описаны, зависимости определены
(БД, композитный снимок, конфиг). Можно приступать к прототипу в `tools/`.
