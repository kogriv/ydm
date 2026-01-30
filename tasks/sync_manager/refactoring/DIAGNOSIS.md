# Диагностика кодовой базы: текущее состояние и влияние задач Smart Diff 2 и Sync Manager

*Дата:* 2026-01-30  
*Контекст:* задачи `tasks/smart_diff_2/` и `tasks/sync_manager/`; рефакторинг перед реализацией фич.

---

## 1. Текущая структура проекта

### 1.1. Монолит `ydm.py` (~2770 строк)

Вся логика в одном файле:

| Компонент | Строки (прибл.) | Назначение |
|-----------|-----------------|------------|
| Глобальные переменные, сигналы, cleanup | 1–95 | Обработка SIGTERM/SIGUSR1, tmpfs cleanup, `load_token_from_env()`, `load_config()` |
| **TerminationRequested** | 94–96 | Исключение для graceful shutdown |
| **StorageManager** | 98–750 | SQLite: `scans`, `files`, `scan_progress`, `disk_info`; tmpfs/checkpoint, init, finalize |
| **LocalScanner** | 752–856 | Обход локальной ФС, запись в БД |
| **YandexClient** | 818–856 | HTTP-запросы к Yandex Disk API |
| **CloudScanner** | 858–989 | Обход облака по API, запись в БД |
| **Analyzer** | 991–~2130 | Композитный снимок, дифф, эвристики полного скана; `get_diff()`, `_compare_simple_scan()`, `_compare_composite_scan()` |
| **YDM_CLI** | 2326–2770 | argparse, command_handler: `init`, `scan`, `report` (в т.ч. `report diff`) |

Конфиг Yandex Disk (`exclude-dirs`) читается только внутри `Analyzer.get_diff()` (строки ~1975–1984): хардкод пути `~/.config/yandex-disk/config.cfg`, парсинг в множество. Отдельного модуля конфига нет.

### 1.2. Внешние зависимости

- **`tools/gen_exclude_list.py`** — отдельный скрипт: токен из `.env`, API корня диска, по белому списку `KEEP_FOLDERS` формирует строку `exclude-dirs=...` для ручного копирования в конфиг. Конфиг не читает и не пишет.
- **`ydm_config.json`** — конфиг ydm (batch size, checkpoint, freshness window, reference_full_scan_id). Читается через `load_config()` в ydm.py.
- **`.env`** — токен Yandex Disk. Читается в ydm.py и в gen_exclude_list.py дублированием логики.

### 1.3. Схема БД (из ydm.py)

- **scans** — id, timestamp, scan_type ('meta'|'cloud'|'local'), status, duration  
- **files** — scan_id, parent_path, name, type, size, md5, created, modified  
- **scan_progress** — scan_id, path, status, offset, total_items, last_checked  
- **disk_info** — scan_id, total_space, used_space, trash_size  

Дерево папок выводится косвенно: по `files` (parent_path, name) и по `scan_progress` (path). Явного «дерева узлов с детьми» для облака нет — есть только `get_folders_in_scan(scan_id)` (множество parent_path).

---

## 2. Логика exclude-dirs и диффа (текущая)

### 2.1. Где используется exclude-dirs

- **Только в `Analyzer.get_diff()`:** при каждом вызове читается конфиг, парсится строка `exclude-dirs=...` в множество (разделитель — запятая, без учёта запятых внутри имён).
- Передаётся в `_compare_simple_scan(cloud_id, local_id, exclude_dirs)` и `_compare_composite_scan(composite, local_id, exclude_dirs)`.

### 2.2. Правило фильтрации (текущее)

- В `_compare_simple_scan` и `_compare_composite_scan` фильтрация **missing_local** делается так:
  - `full_path = f"{parent}/{name}".strip("/")`
  - `root_folder = full_path.split("/")[0]` — только первый сегмент пути
  - строка попадает в вывод, если `root_folder not in exclude_dirs`
- То есть учитывается только **корневая папка** (первый сегмент). Пути вида `video/Blender`, `video/n8n` в конфиге не различаются: если в exclude_dirs есть только `video/Blender`, текущий код всё равно смотрит только на `video` и не исключает по полному пути.
- **missing_cloud** в текущем коде по exclude_dirs не фильтруется (только отсекаются файлы `.sync`).

### 2.3. Вывод для задач

- Для **Smart Diff 2** (sync-only diff) нужна единая корректная логика «путь входит в синхронизируемое подмножество»: путь в sync, если **ни он сам, ни ни один его префикс** (по сегментам) не входит в exclude_dirs. Эту логику нужно использовать для фильтрации и missing_local, и missing_cloud.
- Для **Sync Manager** та же логика нужна для расчёта статусов [S]/[P]/[-] по иерархии и для команд add/inspect. Парсинг и запись exclude-dirs не должны дублироваться — нужен общий модуль конфига.

---

## 3. Влияние задачи Smart Diff 2

**Цель:** `report diff --sync-only` — в выводе только пути из синхронизируемого подмножества.

### 3.1. Затрагиваемый код (только ydm.py)

| Место | Изменение |
|-------|-----------|
| **Analyzer.get_diff()** | Параметр `sync_only=False`; при `sync_only=True` фильтровать и missing_local, и missing_cloud по «path in synced subset». |
| **Analyzer._compare_simple_scan()** | Использовать общую функцию «path in synced subset» вместо проверки только root_folder; при sync_only фильтровать и missing_cloud по тому же правилу. |
| **Analyzer._compare_composite_scan()** | Аналогично: единая проверка по полному пути; при sync_only — фильтрация missing_cloud. |
| **CLI (report)** | Аргумент `--sync-only` у report diff, передача в `get_diff(sync_only=...)`. |

### 3.2. Новая/общая логика

- Функция **«path in synced subset»**: по полному пути (например, `video/n8n/file.txt`) и множеству exclude_dirs (например, `{"video", "video/Blender"}`) вернуть True только если ни путь, ни ни один его префикс не в exclude_dirs. Должна быть в одном месте и использоваться в diff и в sync_manager.

---

## 4. Влияние задачи Sync Manager

**Цель:** команда `sync` (tree, inspect, add, …), работа только со снимками из БД, без сканирования облака; два интерфейса (CLI/JSON и человекочитаемый вывод); метаданные о свежести.

### 4.1. Затрагиваемый код

| Место | Изменение |
|-------|-----------|
| **ydm.py** | Новая команда `sync` (подкоманды tree, inspect, add, …): парсер, ветка в command_handler, вызовы логики. |
| **Дерево облака** | Сейчас: только `build_composite_scan()`, `get_folders_in_scan(scan_id)`. Нужно: построение **полного дерева папок** (узлы с path, name, children или children_count) по снимку из БД (композит или последний полный скан). Данные есть в `files` (parent_path, name, type). |
| **Статусы [S]/[P]/[-]** | Новая логика: по дереву + exclude_dirs вычислить для каждой папки full / partial / excluded (по иерархии). Требует общую функцию «path in synced subset» и правило «есть ли включённые потомки». |
| **Конфиг Yandex Disk** | Чтение и запись `exclude-dirs`: путь к конфигу, парсинг с учётом запятых в путях, запись (add: пересчёт списка исключений, dry-run, --apply). Сейчас парсинг только в get_diff — при реализации sync_manager без рефакторинга будет дублирование и риск расхождений. |

### 4.2. Зависимости Sync Manager от рефакторинга

- **Общий модуль конфига** — чтобы не дублировать парсинг/запись и корректно обрабатывать запятые и опасные символы.
- **Общая функция «path in synced subset»** — для дерева [S]/[P]/[-], для add (проверка пути), для sync-only diff.
- **Построение дерева папок по снимку** — метод/функция «по scan_id или composite получить дерево узлов»; используется в sync tree и sync inspect.

### 4.3. tools/gen_exclude_list.py

- Для первой версии Sync Manager можно не трогать: проверка «папка есть в облаке» и список подпапок берутся из снимка в БД.
- При желании позже: общий парсер конфига позволит gen_exclude_list при необходимости читать текущий exclude-dirs или выводить в том же формате.

---

## 5. Проблемы текущей архитектуры (кратко)

1. **Монолит** — один файл ~2770 строк, сложнее тестировать и расширять.
2. **Парсинг exclude-dirs** только в get_diff, хардкод пути; для sync_manager понадобится ещё запись и аккуратная работа с запятыми — без выноса будет дублирование.
3. **Логика «path в sync»** завязана на первый сегмент пути; для корректного sync-only и дерева [S]/[P]/[-] нужна одна функция по полному пути.
4. **Нет явного «дерева папок облака»** — есть композит и множество parent_path, но нет структуры «узел + дети» для вывода sync tree и расчёта статусов по иерархии.
5. **Дублирование чтения .env** в ydm.py и gen_exclude_list.py (мелочь, но при рефакторинге можно унифицировать точку входа для токена).

---

## 6. Сводная таблица затронутых компонентов

| Компонент | Smart Diff 2 | Sync Manager |
|-----------|---------------|--------------|
| **ydm.py (Analyzer)** | get_diff, _compare_*, общая проверка «in sync» | Дерево по снимку, статусы [S]/[P]/[-] (через общую «in sync») |
| **ydm.py (CLI)** | report diff --sync-only | Команда sync (tree, inspect, add) |
| **Конфиг exclude-dirs** | Чтение уже есть в get_diff | Чтение + запись, парсинг запятых — нужен общий модуль |
| **Дерево облака** | Не требуется | Нужно построение из БД (композит/полный скан) |
| **tools/gen_exclude_list.py** | Не затронут | Опционально: общий парсер конфига |

Итог: рефакторинг (общий конфиг, общая «path in sync», слой снимков/дерева, разбиение на подпакеты в отдельном каталоге кода) упростит и Smart Diff 2, и Sync Manager и снизит риск дублирования и ошибок.

**Целевая структура после рефакторинга:** код в каталоге `src/ydm/` с подпакетами config, storage, scan, analysis, sync, cli; точка входа `ydm.py` в корне; слои и паттерны — без излишеств. Детали: [REFACTORING_PLAN.md](REFACTORING_PLAN.md), [ARCHITECTURE.md](ARCHITECTURE.md).
