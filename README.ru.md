*Read this in [English](README.md).*

# YDM - Yandex Disk Monitor

Инструмент для глубокого анализа состояния Яндекс.Диска, сверки локальной копии с облаком и отслеживания динамики изменений.

## Быстрая памятка (алиасы)

```bash
ydm
ydm-menu
ydm-scan-cloud
ydm-scan-cloud /Books/Math
ydm-scan-cloud-path /video
ydm-scan-local
ydm-tree
ydm-tree-path /video 3
ydm-sync-add /Projects/2024
ydm-sync-pick /Books/Math
ydm-sync-state
ydm-sync-rm /Projects/2024
ydm-help
```

Для rclone/bisync на Android есть policy-aware слой, который отделяет
download-only зеркала от bidirectional путей:

```bash
python3 tools/sync_policy.py status --local-root /sdcard/Download/ya_disk
python3 tools/sync_policy.py inspect --path /pro/agents --local-root /sdcard/Download/ya_disk
python3 tools/sync_policy.py render-filters --local-root /sdcard/Download/ya_disk --apply
python3 tools/sync_rename.py plan --local-root /sdcard/Download/ya_disk --old /DAO/a.txt --new /DAO/b.txt
python3 tools/sync_rename.py apply --local-root /sdcard/Download/ya_disk --old /DAO/a.txt --new /DAO/b.txt
```

## Features

- 🔍 **Полное сканирование облака** - рекурсивный обход всех файлов и папок через Yandex Disk API
- 💾 **Локальное сканирование** - сканирование локальной файловой системы
- 📊 **Сравнение облако vs локально** - выявление расхождений и проблем синхронизации
- 🔄 **Resumable сканирование** - возможность прервать и продолжить сканирование с места остановки
- ⚡ **Оптимизированная производительность** - использование tmpfs для быстрой работы с большими объемами данных
- 📈 **Детальная аналитика** - поиск дубликатов, длинных путей, анализ структуры

## Requirements

- Python 3.9+ (утилиты `sync_*` используют `argparse.BooleanOptionalAction`,
  появившийся в 3.9 — проверяется в CI на 3.9/3.11/3.13)
- Один из двух способов доступа к Yandex Disk:
  - **API-бэкенд (по умолчанию)** — токен Yandex Disk OAuth (получить можно
    [здесь](https://yandex.ru/dev/disk/poligon/)); нужен также демон
    `yandex-disk` для `scan local`/`report diff`/управления синком.
  - **rclone-бэкенд** (`--backend rclone`) — авторизованный remote в
    `rclone.conf` (`rclone config`), без демона и без `.env`. Нужен для
    окружений, где официальный `yandex-disk` не работает (например, arm64 —
    подробности и весь набор инструментов в
    [`tasks/rclone_backend/README.md`](tasks/rclone_backend/README.md);
    учтите, что это конкретный рецепт под одно Android/Termux-устройство,
    см. дисклеймер в начале того файла).

## Installation

1. Клонируйте репозиторий:
```bash
git clone <repository-url>
cd ydm
```

2. Настройте доступ (один из двух):
```bash
# Вариант А — API-бэкенд: файл .env
echo "YANDEX_DISK_TOKEN=your_token_here" > .env

# Вариант Б — rclone-бэкенд: remote в rclone.conf, .env не нужен
rclone config   # storage> yandex
```

3. (Опционально) Настройте конфигурацию в `ydm_config.json`:
```json
{
  "prod": {
    "cloud_batch_size": 500,
    "checkpoint_files_threshold": 10000,
    "checkpoint_time_sec": 300
  }
}
```

## Quick Start

### Инициализация базы данных
```bash
python3 ydm.py init
```

### Сканирование облака
```bash
# Полное сканирование с прогрессом
python3 ydm.py scan cloud --progress

# Сканирование конкретной папки
python3 ydm.py scan cloud --path "/Архив" --progress

# Продолжить прерванное сканирование
python3 ydm.py scan cloud --resume --progress
```

### Сканирование локальной файловой системы
```bash
python3 ydm.py scan local --path /path/to/local/directory
```

### Получение отчетов
```bash
# Статус последних сканов
python3 ydm.py report status

# Сравнение облако vs локально
python3 ydm.py report diff

# Детальная информация о скане
python3 ydm.py report scan-info --scan-id 5

# Список всех сканов
python3 ydm.py report scan-list --limit 20
```

## Configuration

### Переменные окружения

Создайте файл `.env` в корне проекта:
```
YANDEX_DISK_TOKEN=your_oauth_token_here
```

### Конфигурационные профили

Проект поддерживает профили конфигурации в `ydm_config.json`:

- **prod** (по умолчанию) - для стабильной работы с редкими checkpoints
- **test** - для быстрого тестирования с частыми checkpoints

Использование:
```bash
# Production профиль (по умолчанию)
python3 ydm.py scan cloud --progress

# Test профиль (быстрые checkpoints)
python3 ydm.py --config-profile test scan cloud --progress
```

## Usage Examples

### Полный workflow сканирования

```bash
# 1. Начать новое сканирование
python3 ydm.py scan cloud --progress 2>&1 | tee scan.log &

# 2. Мониторить прогресс
tail -f scan.log | jq -r 'select(.status=="progress") | "\(.scanned) files - \(.current)"'

# 3. При необходимости - безопасно прервать (Ctrl+C или kill -TERM)
# Данные автоматически сохранятся на диск

# 4. Продолжить позже
python3 ydm.py scan cloud --resume --progress
```

### Сравнение облако vs локально

```bash
# 1. Сканировать облако
python3 ydm.py scan cloud --progress

# 2. Сканировать локальную копию
python3 ydm.py scan local --path /data/ya_disk

# 3. Сравнить результаты
python3 ydm.py report diff
```

### Поиск проблем

```bash
# Найти файлы с длинными путями (>240 символов)
python3 ydm.py report long-paths --scan-id 5 --limit-chars 240

# Найти дубликаты файлов
python3 ydm.py report duplicates --scan-id 5

# Анализ целостности скана
python3 ydm.py report analyze-scan --scan-id 5
```

## Simple Sync Tools (переходные)

Переходные CLI‑утилиты для управления синхронизацией через `exclude-dirs`
и просмотра дерева синхронизации (работают по снимку из `monitor.db`,
без сканирования облака во время выполнения).

### Просмотр дерева синхронизации
```bash
# Свернутое дерево (JSON по умолчанию)
python3 tools/sync_tree.py --path /Projects --depth 2

# Полное дерево с ветками (text)
python3 tools/sync_tree.py --path /Projects --depth 2 --format text --text-tree --show-all

# Без заголовка в text
python3 tools/sync_tree.py --format text --text-tree --no-text-header

# Отключить локальный скан и sync_percent
python3 tools/sync_tree.py --no-local-scan --no-sync-percent
```

### Управление exclude-dirs
```bash
# Список exclude-dirs
python3 tools/sync_exclude.py list

# Dry-run: включить подпапку
python3 tools/sync_exclude.py add --path /Projects/2024

# Применить изменения
python3 tools/sync_exclude.py add --path /Projects/2024 --apply

# Исключить папку
python3 tools/sync_exclude.py remove --path /Projects/2024 --apply

# Текстовый вывод без заголовка
python3 tools/sync_exclude.py add --path /Projects/2024 --format text --no-text-header

# Отключить рестарт демона и локальный скан
python3 tools/sync_exclude.py add --path /Projects/2024 --apply --no-restart-daemon --no-local-scan
```

### Управление синком без демона (`--backend rclone`)

Для окружений без `yandex-disk` (см. [Rclone Backend](tasks/rclone_backend/README.md))
`tools/sync_exclude.py` заменяется на `tools/sync_filters.py` — тот же UX
(dry-run по умолчанию, `--apply` для применения), но вместо правки
`config.cfg`+рестарта демона — правка rclone filter-file + `rclone copy`
материализация:

```bash
# Список включённых в синк папок
python3 tools/sync_filters.py list --local-root /path/to/local/mirror

# Dry-run: включить папку
python3 tools/sync_filters.py add --path /Projects --local-root /path/to/local/mirror

# Применить — материализует локально через rclone copy
python3 tools/sync_filters.py add --path /Projects --local-root /path/to/local/mirror --apply

# Убрать из синка; --delete-local чистит содержимое только после
# чистого `rclone check` (0 расхождений)
python3 tools/sync_filters.py remove --path /Projects --local-root /path/to/local/mirror --apply --delete-local
```

`tools/sync_tree.py` тоже поддерживает `--backend rclone` (по умолчанию —
`api`, поведение не меняется): при `--backend rclone` дерево строится по
filter-file вместо `config.cfg`.

JSON‑контракт (версии):
- `sync_tree` → `"schema": "sync_tree:v1"`
- `sync_exclude` → `"schema": "sync_exclude:v1"`
- `sync_filters` → `"schema": "sync_filters:v1"`

Примечания:
- По умолчанию `sync_tree` запускает локальный скан и считает `sync_percent`.
- По умолчанию `sync_exclude --apply` перезапускает демон и запускает локальный скан.
- `sync_filters --apply` ничего не перезапускает (демона нет) — сразу гоняет `rclone copy`.

## Алиасы (system ~/.bashrc)

### Что добавлено
Алиасы и функции добавлены в `~/.bashrc` для частых сценариев:
- `ydm-scan-cloud` — полный cloud scan
- `ydm-scan-cloud <path>` — cloud scan одной папки
- `ydm-scan-cloud-path <path>` — cloud scan папки
- `ydm-scan-local` — local scan для `/data/ya_disk`
- `ydm-tree` — дерево синка (text + ветки)
- `ydm-tree-path <path> [depth]` — дерево для папки с глубиной
- `ydm-sync-add <path>` — добавить папку как `bidirectional`, если risk analyzer
  считает путь безопасным; после изменения фильтра команда сама покажет
  короткий статус и спросит, запускать ли `ydm-bisync-resync --apply`
- `ydm-sync-add --mode <mode> <path>` — явный режим:
  `bidirectional`, `download_only` или `disabled`
- `ydm-sync-pick <parent>` — интерактивно выбрать подпапку по номеру из
  `rclone lsf`, чтобы не вводить кириллицу вручную; после выбора запускает
  обычный `ydm-sync-add <path>`
- `ydm-sync-state` — короткий пользовательский статус sync и следующий шаг
- `ydm-sync-rm <path>` — убрать папку из sync

Если `ydm-sync-add` пишет `BLOCKED` с `Risk: path_not_found`, это значит, что
путь есть в облаке, но его ещё нет в актуальном cloud snapshot `ydm`. Сначала
обновите снимок:

```bash
ydm-scan-cloud /Books/Math/База
```

Потом повторите `ydm-sync-add` или `ydm-sync-pick`.
- `ydm-help` — краткая подсказка с постраничным выводом через `less`, если
  доступен интерактивный терминал
- `ydm-help --plain` или `ydm-help --no-pager` — напечатать подсказку без pager
- Текст `ydm-help` намеренно ASCII-only и с короткими строками для узкого
  экрана Termux/proot.

### Termux/proot scroll

Если в Termux при попытке протянуть экран пальцем листается история команд в
строке ввода, а не scrollback, для длинной справки используйте pager:

```bash
ydm-help
```

Внутри `less`:

- `q` — выйти
- `Space` / `b` — страница вниз / вверх
- `j` / `k` — строка вниз / вверх
- `/text` — поиск

Если scroll залип после TUI/pager и свайп продолжает работать как стрелки
вверх/вниз, сбросьте режим терминала:

```bash
termux-scroll-fix
```

`ydm-help --plain` оставлен для случаев, когда вывод надо передать в pipe или
скопировать целиком.

### Важно
- `ydm-sync-add` и `ydm-sync-rm` **выполняют `--apply` напрямую**.  
  Это значит, что изменение `exclude-dirs` применяется сразу, а затем
  запускается рестарт демона и локальный скан (по умолчанию в `sync_exclude`).

### Примеры
```bash
ydm-tree-path /video 3
ydm-scan-cloud-path /Projects
ydm-sync-add /Projects/2024
```

### Как применить
```bash
source ~/.bashrc
```
## Documentation

- **[PROJECT_YD_MONITOR.md](docs/PROJECT_YD_MONITOR.md)** - Полная документация проекта, архитектура, детали реализации
- **[QUICKSTART_AI.md](docs/QUICKSTART_AI.md)** - Быстрый старт для AI-ассистентов и автоматизации
- **[USAGE_EXAMPLES.md](docs/USAGE_EXAMPLES.md)** - Дополнительные примеры использования
- **[Sync Manager](tasks/sync_manager/README.md)** - Переходные инструменты sync_tree/sync_exclude и планы Sync Manager
- **[Rclone Backend](tasks/rclone_backend/README.md)** - Альтернатива демону `yandex-disk` для окружений без него (arm64/Android): `RcloneBackend`, `sync_filters.py`, junk cleanup через rclone
- **[Smart Diff](tasks/smart_diff/README.md)** - Как `report diff` строит композитный снимок (полный скан + свежие частичные) вместо сравнения двух последних сканов
- **[CHANGELOG.md](CHANGELOG.md)** - Заметные изменения, начиная с последних
- **[Known Issues](docs/KNOWN_ISSUES.md)** - Текущие неисправленные ограничения

## Файловая структура проекта

Основные директории и файлы:

- `ydm.py` — основной CLI-инструмент (инициализация БД, сканы, отчеты)
- `ydm_config.json` — конфигурация профилей (prod/test)
- `monitor.db` — основная SQLite-база (результаты сканирования)
- `README.md`, `LICENSE`, `.env.example` — документация и пример конфига

Папки верхнего уровня:

- `docs/` — общая документация по проекту
  - `PROJECT_YD_MONITOR.md` — архитектура и детали реализации
  - `QUICKSTART_AI.md` — быстрый старт для AI/скриптов
  - `USAGE_EXAMPLES.md` — примеры использования
  - `KNOWN_ISSUES.md` — текущие неисправленные ограничения
- `tasks/` — задачи/подпроекты поверх ядра
  - `tasks/junk/` — задача очистки мусора:
    - `plan_cleanup.py` — генерация плана удаления (`var/junk_list.txt`)
    - `run_cleanup.py` — выполнение плана (`--backend api|rclone`, `var/deleted.log`)
    - `smart_clean.py` — комбинированный скрипт анализа и очистки
    - `analyze_junk.py`, `CLEANUP_GUIDE.md` — аналитика и документация по cleanup
  - `tasks/smart_diff/` — композитный снимок для diff (см. Documentation выше)
  - `tasks/long_names/` — AI-переименование длинных имён, незавершённый
    прототип (`smart_renamer.py`) — статус в его README
  - `tasks/rclone_backend/` — альтернатива демону `yandex-disk` через rclone
    (для окружений вроде arm64, где официальный клиент не работает)
- `tools/` — вспомогательные утилиты
  - `gen_exclude_list.py` — генерация строки `exclude-dirs=` для конфига Yandex.Disk
  - `sync_tree.py` — дерево синхронизации по снимку (JSON/text; `--backend api|rclone`)
  - `sync_exclude.py` — add/remove/list для `exclude-dirs` (демон, dry-run по умолчанию)
  - `sync_filters.py` — add/remove/list для rclone filter-file (без демона, dry-run по умолчанию)
  - `sync_common.py` — общий код для sync‑утилит
- `tests/` — тестовые скрипты:
  - `test_scan.sh` — интеграционный тест сканирования с tmpfs
  - `test_ydm_fixes.sh` — набор регрессионных тестов для `ydm.py`
- `assets/` — медиа/схемы:
  - `poligon_endpoints.png` — схема эндпоинтов (исторически)
- `var/` — рабочие данные и артефакты:
  - `junk_list.txt` — текущий план очистки
  - `deleted.log` — журнал фактически удаленных путей
  - вспомогательные файлы (`*.db`, `*.json`, `*.old`) — временные и диагностические данные

## Architecture

Проект использует двухуровневое хранилище для баланса производительности и безопасности:

1. **RAM DB (tmpfs)** - временное хранилище в памяти для быстрого сканирования
2. **Disk DB (monitor.db)** - постоянное хранилище с периодическими checkpoints

Подробнее об архитектуре см. [PROJECT_YD_MONITOR.md](docs/PROJECT_YD_MONITOR.md).

## Command Reference

### Global Flags
- `--db-path PATH` - путь к базе данных (по умолчанию: `monitor.db`)
- `--format {text|json}` - формат вывода (по умолчанию: `text`)
- `--config-profile {prod|test}` - профиль конфигурации (по умолчанию: `prod`)
- `--backend {api|rclone}` - источник данных для `scan meta`/`scan cloud`
  (по умолчанию: `api`, требует `YANDEX_DISK_TOKEN`; `rclone` — через
  `rclone.conf`, см. [`tasks/rclone_backend/README.md`](tasks/rclone_backend/README.md))

### Scan Commands
- `scan meta` - быстрая метаинформация о диске
- `scan cloud [--path PATH] [--progress] [--resume] [--scan-id ID]` - сканирование облака
- `scan local [--path PATH]` - сканирование локальной файловой системы

### Report Commands
- `report status` - последние сканы
- `report diff` - сравнение cloud vs local (композитный снимок по умолчанию, см. [Smart Diff](tasks/smart_diff/README.md))
- `report scan-list [--limit N]` - список всех сканов
- `report scan-info --scan-id ID` - детали скана
- `report scan-progress --scan-id ID` - прогресс скана
- `report long-paths --scan-id ID [--limit-chars N]` - файлы с длинными путями
- `report duplicates --scan-id ID [--by-hash|--by-name]` - поиск дубликатов
- `report clean-duplicates [--scan-id ID]` - убрать дублирующиеся строки от старого бага (см. [CHANGELOG.md](CHANGELOG.md))
- `report analyze-scan --scan-id ID` - анализ целостности
- `report full-scan-info` / `report full-scan-candidates` - какой скан используется как база композитного diff и почему

Полный список команд: `python3 ydm.py --help`

## License

MIT License - см. [LICENSE](LICENSE) файл для деталей.

## Contributing

Вклад приветствуется! См. [CONTRIBUTING.md](CONTRIBUTING.md).

## Known Issues

См. [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md) — сейчас там одна:
SIGTERM во время инициализации скана (до старта основного цикла) может
подвесить процесс; в этом случае используйте `kill -9`.

## Notes

- Проект использует только стандартную библиотеку Python (stdlib), без внешних зависимостей
- Все данные хранятся локально в SQLite базе данных
- Токены и секреты никогда не коммитятся в репозиторий (используйте `.env` файл)
