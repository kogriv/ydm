# План структурного и архитектурного рефакторинга

*Дата:* 2026-01-30  
*Подход:* сначала полноценный рефакторинг кода, затем реализация задач Smart Diff 2 и Sync Manager.  
*Промежуточно:* реализованы переходные CLI‑тулы `tools/sync_tree.py` и `tools/sync_exclude.py` (см. `tasks/sync_manager/simple_sync/`) для быстрой работы без рефакторинга.
*Принципы:* код в отдельном каталоге (src), разбиение на подпакеты, лёгкие паттерны для расширяемости, без оверхеда.

Архитектурные слои и паттерны описаны в [ARCHITECTURE.md](ARCHITECTURE.md).

---

## 1. Цели рефакторинга

1. **Разделение кода и корня проекта** — исходники в `src/ydm/`, точка входа и конфиги в корне.
2. **Разделение ответственности** — хранение, сканирование, анализ, конфиг, sync-логика, CLI в отдельных подпакетах.
3. **Единые точки** — один модуль конфига Yandex Disk; одна функция «path in synced subset» для diff и sync_manager.
4. **Расширяемость** — добавление команд и отчётов без раздувания одного файла; чёткие зависимости между слоями.
5. **Тестируемость** — модули и доменная логика (чистые функции) тестируются по отдельности.

---

## 2. Целевая структура: каталог кода и подпакеты

Код приложения в **`src/ydm/`**. Подпакеты сгруппированы по зонам ответственности.

```
<корень проекта>/
  ydm.py                    # Точка входа: sys.path + вызов ydm.cli.main()
  ydm_config.json
  .env
  src/
    ydm/
      __init__.py           # Версия, при необходимости re-exports для внешнего использования
      config/               # Конфиг приложения и окружение
        __init__.py
        app_config.py       # DEFAULT_CONFIG, load_config(profile) — ydm_config.json
        env.py              # load_token_from_env() — .env
      storage/              # Персистентность
        __init__.py
        manager.py          # StorageManager — SQLite, tmpfs, checkpoint, init, finalize
      scan/                  # Сканирование облака и локальной ФС
        __init__.py
        client.py           # YandexClient — HTTP к API
        cloud.py            # CloudScanner
        local.py            # LocalScanner
      analysis/              # Анализ снимков, отчёты, композит, дифф
        __init__.py
        analyzer.py         # Analyzer: composite, find_last_full_scan, get_diff, get_scan_*, heuristics
        tree.py             # Построение дерева папок по снимку (по composite/scan_id)
      sync/                  # Профиль синхронизации (exclude-dirs) и логика sync
        __init__.py
        config.py           # Yandex Disk config.cfg: get_exclude_dirs(), set_exclude_dirs()
        logic.py            # is_path_synced(), compute_sync_status(), build_tree_with_sync_status()
      cli/                   # Интерфейс командной строки
        __init__.py
        parser.py           # Построение argparse (init, scan, report, sync)
        runner.py           # Обработчики run_init, run_scan, run_report, run_sync
        render.py           # Вывод JSON/text (опционально отдельный модуль)
  tests/
  tasks/
  tools/
```

Точка входа **`ydm.py`** в корне (минимальный скрипт):

```python
# ydm.py
import sys
from pathlib import Path
src = Path(__file__).resolve().parent / "src"
if str(src) not in sys.path:
    sys.path.insert(0, str(src))
from ydm.cli.runner import main
if __name__ == "__main__":
    main()
```

Либо установка пакета (`pip install -e .`) и вызов `python -m ydm` — тогда `src` указывается в `pyproject.toml` как пакетный корень.

---

## 3. Назначение подпакетов и модулей

### 3.1. ydm.config

- **app_config.py** — `DEFAULT_CONFIG`, `load_config(profile="prod")` по ydm_config.json. Без зависимостей от storage/scan/analysis.
- **env.py** — `load_token_from_env()` по .env. Общая точка для ydm и, при желании, tools/gen_exclude_list. Без зависимостей от остальных модулей ydm.

### 3.2. ydm.storage

- **manager.py** — класс **StorageManager** (перенос из текущего ydm.py): init_db, get_connection, start_scan, finish_scan, save_disk_info, save_file, get_pending_folders, update_folder_status, save_checkpoint, get_scan_details, get_scan_stats, checkpoint_to_disk, finalize, restore_from_disk, recover_crashed_scans, add_folders_to_scan и т.д. Зависимости: sqlite3, os, time, shutil; конфиг (пороги checkpoint) передаётся снаружи. Глобальные переменные для сигналов не в storage — остаются в cli/runner при необходимости.

### 3.3. ydm.scan

- **client.py** — **YandexClient** (HTTP к Yandex Disk API).
- **cloud.py** — **CloudScanner** (обход облака, вызов storage.save_file и т.д.). Зависит от storage (интерфейс), config (batch size), env или переданный токен.
- **local.py** — **LocalScanner** (обход локальной ФС, запись в storage). Зависит от storage.

### 3.4. ydm.analysis

- **analyzer.py** — класс **Analyzer**: build_composite_scan, find_last_full_scan, find_partial_scans_after, get_folders_in_scan, get_diff, _compare_simple_scan, _compare_composite_scan, get_scan_metadata, is_full_scan, get_long_paths, get_scan_info, get_status, get_scan_progress, дубликаты, full-scan heuristics. **Не читает exclude-dirs сам** — получает `exclude_dirs` снаружи (передаёт runner из sync.config). Для «path in synced subset» использует `ydm.sync.logic.is_path_synced`.
- **tree.py** — построение дерева папок по снимку (по composite или scan_id): функция или метод `get_cloud_folder_tree(storage, composite_or_scan_id)` → структура узлов (path, name, children или children_count). Данные из storage (files). Может быть методом Analyzer или отдельной функцией, принимающей storage и снимок.

### 3.5. ydm.sync

- **config.py** — конфиг Yandex Disk (config.cfg): путь из переменной окружения или `~/.config/yandex-disk/config.cfg`. `get_exclude_dirs()` — чтение и парсинг с учётом запятых в путях. `set_exclude_dirs(paths)` — запись (бэкап, форматирование). Без зависимостей от storage/scan/analysis.
- **logic.py** — доменная логика sync (чистые функции где возможно):
  - **is_path_synced(full_path: str, exclude_dirs: Set[str]) -> bool** — путь входит в синхронизируемое подмножество (ни он, ни его префикс не в exclude_dirs).
  - **compute_sync_status(path, exclude_dirs, children_sync_statuses)** — для одной папки: full / partial / excluded ([S]/[P]/[-]).
  - **build_tree_with_sync_status(folder_tree, exclude_dirs, max_depth=None, root_path="/")** — дерево узлов с полями path, name, sync_status, children/children_count. Использует данные дерева из analysis.tree и exclude_dirs из sync.config.

### 3.6. ydm.cli

- **parser.py** — построение ArgumentParser: init, scan (meta/local/cloud), report (status, diff, scan-info, …), sync (tree, inspect, add). Флаги --sync-only, --path, --depth, --format, --apply и т.д.
- **runner.py** — обработчики: `run_init(storage)`, `run_scan(args, storage, config, ...)`, `run_report(args, storage, analyzer, sync_config, ...)`, `run_sync(args, storage, analyzer, sync_config, sync_logic, ...)`. Глобальные переменные для сигналов и cleanup tmpfs — здесь или в одном месте при старте. Вызов парсера и передача args в нужный run_*.
- **render.py** (опционально) — вывод в JSON/text по args.format. Можно оставить внутри runner.

Зависимости cli: config, storage, scan, analysis, sync (config + logic). CLI создаёт storage, analyzer, читает sync_config и передаёт их в run_*.

---

## 4. Паттерны (кратко)

- **Передача зависимостей:** Analyzer(storage), run_report(..., analyzer, sync_config). exclude_dirs в get_diff передаётся снаружи (из sync_config.get_exclude_dirs()).
- **Чистые функции в sync.logic:** is_path_synced, compute_sync_status — без I/O, легко тестировать.
- **Единые точки входа команд:** run_init, run_scan, run_report, run_sync — CLI только парсит и вызывает.
- **Слои:** infrastructure (storage, scan, config) → application (analysis, sync.logic) → presentation (cli). Подробнее — [ARCHITECTURE.md](ARCHITECTURE.md).

---

## 5. Порядок выполнения рефакторинга

После каждого шага проверять: init, scan meta/local/cloud, report status/diff/scan-info работают.

### Фаза 1: Каталог src и база

1. Создать **src/ydm/** и подпакеты (config, storage, scan, analysis, sync, cli) с `__init__.py`.
2. **ydm.config** — перенести DEFAULT_CONFIG и load_config в app_config.py, load_token_from_env в env.py. Точка входа ydm.py в корне: sys.path + `from ydm.cli.runner import main; main()`.
3. **ydm.storage** — перенести StorageManager в storage/manager.py. В runner (пока в старом ydm.py или уже в cli/runner.py) импорт из ydm.storage. Проверка: init, scan local (короткий путь), report status.
4. **ydm.scan** — перенести YandexClient в client.py, LocalScanner в local.py, CloudScanner в cloud.py. Импорт в runner. Проверка: scan meta, local, cloud.
5. **ydm.analysis** — перенести Analyzer в analyzer.py; чтение exclude_dirs оставить временно внутри get_diff (как сейчас) или сразу передавать извне. Добавить tree.py с построением дерева папок по снимку. Импорт в runner. Проверка: report status, diff, scan-info, full-scan-info.
6. **ydm.cli** — перенести парсер в parser.py, command_handler в runner.py (run_init, run_scan, run_report). Сигналы и tmpfs cleanup оставить в runner. Проверка: все текущие команды работают через ydm.py.

### Фаза 2: Конфиг Yandex Disk и sync-логика

7. **ydm.sync.config** — реализовать get_exclude_dirs(), set_exclude_dirs(paths). В run_report для diff брать exclude_dirs из sync.config.get_exclude_dirs() и передавать в analyzer.get_diff(..., exclude_dirs=...). Убрать чтение конфига из Analyzer.
8. **ydm.sync.logic** — реализовать is_path_synced(). В Analyzer в _compare_simple_scan и _compare_composite_scan использовать is_path_synced вместо проверки по root_folder. Проверка: report diff без регрессий.
9. **ydm.sync.logic** — добавить compute_sync_status и build_tree_with_sync_status (дерево из analysis.tree + exclude_dirs → узлы с sync_status). Проверка: вызов из теста или временно из report.

### Фаза 3: Команда sync и подготовка к фичам

10. **ydm.cli** — в parser добавить команду sync (tree, inspect, add); в runner — run_sync(args, ...) с вызовом build_tree_with_sync_status и выводом (пока минимальный). Проверка: ydm sync tree --format json не падает.

После фазы 3 рефакторинг готов к реализации фич: Smart Diff 2 (--sync-only) и полные сценарии sync tree/inspect/add.

---

## 6. Реализация фич после рефакторинга

### 6.1. Smart Diff 2

- В **analyzer.get_diff()** добавить параметр `sync_only=False`. При `sync_only=True` фильтровать missing_local и missing_cloud через sync.logic.is_path_synced. exclude_dirs передаются из run_report (sync.config.get_exclude_dirs()).
- В **parser** добавить флаг report diff `--sync-only`; в run_report передавать в get_diff(sync_only=...).

### 6.2. Sync Manager

- **sync tree** — run_sync вызывает analysis.tree + sync.logic.build_tree_with_sync_status, вывод по --format (JSON/text с [S]/[P]/[-]), --path, --depth. Метаданные о свежести снимка из analyzer.
- **sync inspect** — поддерево по --path, проверка опасных имён (логика из TASK_SYNC_MANAGER), вывод с предупреждениями.
- **sync add** — проверка папки в снимке (БД), расчёт нового exclude-dirs (sync.logic или отдельная функция), dry-run по умолчанию, запись по --apply через sync.config.set_exclude_dirs. Метаданные о свежести после изменения.
  - Прототипы уже доступны как `tools/sync_tree.py` и `tools/sync_exclude.py` (см. `tasks/sync_manager/simple_sync/`).

---

## 7. Расширяемость

- Новые отчёты — в ydm.analysis и ветка в run_report; новые подкоманды sync — в ydm.sync.logic и run_sync.
- Другой источник exclude_dirs — замена только ydm.sync.config при сохранении интерфейса get/set.
- Второй бэкенд хранилища — при необходимости абстракция над StorageManager; пока одного достаточно.

---

## 8. Риски и ограничения

- **Обратная совместиность CLI** — имена команд и флагов не менять; добавлять только новые (--sync-only, sync tree/inspect/add).
- **Путь к пакету** — при запуске через ydm.py в корне sys.path должен содержать src; при установке пакета — пакет установлен в окружение.
- **Глобальное состояние** — сигналы и tmpfs cleanup в cli.runner; storage и scanners без глобального состояния между вызовами (кэш в Analyzer допустим).

---

## 9. Краткая последовательность

| Этап | Действие |
|------|----------|
| 1 | Создать src/ydm/ и подпакеты config, storage, scan, analysis, sync, cli |
| 2 | config (app_config, env), точка входа ydm.py с sys.path |
| 3 | storage.manager — StorageManager |
| 4 | scan (client, cloud, local) — YandexClient, CloudScanner, LocalScanner |
| 5 | analysis (analyzer, tree) — Analyzer, построение дерева по снимку |
| 6 | cli (parser, runner) — перенос парсера и обработчиков |
| 7 | sync.config — get/set exclude-dirs; убрать чтение конфига из Analyzer |
| 8 | sync.logic — is_path_synced, compute_sync_status, build_tree_with_sync_status |
| 9 | run_sync, парсер sync (tree, inspect, add) |
| 10 | **Фичи:** report diff --sync-only; sync tree, inspect, add |

Документы: [DIAGNOSIS.md](DIAGNOSIS.md) (диагностика кода и влияния задач), [ARCHITECTURE.md](ARCHITECTURE.md) (слои и паттерны).
