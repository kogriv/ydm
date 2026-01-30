# YDM - Yandex Disk Monitor

Инструмент для глубокого анализа состояния Яндекс.Диска, сверки локальной копии с облаком и отслеживания динамики изменений.

## Features

- 🔍 **Полное сканирование облака** - рекурсивный обход всех файлов и папок через Yandex Disk API
- 💾 **Локальное сканирование** - сканирование локальной файловой системы
- 📊 **Сравнение облако vs локально** - выявление расхождений и проблем синхронизации
- 🔄 **Resumable сканирование** - возможность прервать и продолжить сканирование с места остановки
- ⚡ **Оптимизированная производительность** - использование tmpfs для быстрой работы с большими объемами данных
- 📈 **Детальная аналитика** - поиск дубликатов, длинных путей, анализ структуры

## Requirements

- Python 3.6+
- Токен Yandex Disk OAuth (получить можно [здесь](https://yandex.ru/dev/disk/poligon/))

## Installation

1. Клонируйте репозиторий:
```bash
git clone <repository-url>
cd ydm
```

2. Создайте файл `.env` в корне проекта:
```bash
echo "YANDEX_DISK_TOKEN=your_token_here" > .env
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

## Documentation

- **[PROJECT_YD_MONITOR.md](docs/PROJECT_YD_MONITOR.md)** - Полная документация проекта, архитектура, детали реализации
- **[QUICKSTART_AI.md](docs/QUICKSTART_AI.md)** - Быстрый старт для AI-ассистентов и автоматизации
- **[USAGE_EXAMPLES.md](docs/USAGE_EXAMPLES.md)** - Дополнительные примеры использования

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
  - `issues/` — исторические отчеты и разбор проблем
  - `long_names/` — вспомогательные материалы для задачи длинных путей
- `tasks/` — задачи/подпроекты поверх ядра
  - `tasks/junk/` — задача очистки мусора:
    - `plan_cleanup.py` — генерация плана удаления (`var/junk_list.txt`)
    - `run_cleanup.py` — выполнение плана (удаление через API, `var/deleted.log`)
    - `smart_clean.py` — комбинированный скрипт анализа и очистки
    - `analyze_junk.py`, `CLEANUP_GUIDE.md`, `JUNK_REPORT.md` — аналитика и документация по cleanup
  - `tasks/long_names/` — задача про длинные пути:
    - `ISSUE_LONG_FILENAMES.md`, `RESULTS_AND_PLAN.md`
- `tools/` — вспомогательные утилиты
  - `gen_exclude_list.py` — генерация строки `exclude-dirs=` для конфига Yandex.Disk
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

### Scan Commands
- `scan meta` - быстрая метаинформация о диске
- `scan cloud [--path PATH] [--progress] [--resume] [--scan-id ID]` - сканирование облака
- `scan local [--path PATH]` - сканирование локальной файловой системы

### Report Commands
- `report status` - последние сканы
- `report diff` - сравнение cloud vs local
- `report scan-list [--limit N]` - список всех сканов
- `report scan-info --scan-id ID` - детали скана
- `report scan-progress --scan-id ID` - прогресс скана
- `report long-paths --scan-id ID [--limit-chars N]` - файлы с длинными путями
- `report duplicates --scan-id ID [--by-hash|--by-name]` - поиск дубликатов
- `report analyze-scan --scan-id ID` - анализ целостности

Полный список команд: `python3 ydm.py --help`

## License

MIT License - см. [LICENSE](LICENSE) файл для деталей.

## Contributing

Вклад приветствуется! Пожалуйста, создавайте issues и pull requests.

## Notes

- Проект использует только стандартную библиотеку Python (stdlib), без внешних зависимостей
- Все данные хранятся локально в SQLite базе данных
- Токены и секреты никогда не коммитятся в репозиторий (используйте `.env` файл)

