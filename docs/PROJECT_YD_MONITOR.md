# Проект: Yandex Disk Smart Monitor & Audit

**Цель:** Создать инструмент для глубокого анализа состояния Яндекс.Диска, сверки локальной копии с облаком и отслеживания динамики изменений.

**🚀 Быстрый старт для AI и автоматизации:** См. [QUICKSTART_AI.md](QUICKSTART_AI.md)

---

## 1. Архитектура

### Компоненты
1.  **Сборщик (Collector):** Модуль сбора данных из API Яндекс.Диска и локальной файловой системы.
2.  **Хранилище (Storage):** SQLite база данных для хранения снимков состояния.
3.  **Анализатор (Analyzer):** Модуль сравнения снимков (Облако vs Локально, Вчера vs Сегодня).
4.  **CLI Интерфейс:** Команды управления.

### Переходные CLI‑утилиты (simple_sync)
Для управления синхронизацией без рефакторинга добавлены отдельные тулзы:
- `tools/sync_tree.py` — дерево синхронизации по снимку из `monitor.db`,
  с опциональным локальным сканом и расчётом `sync_percent`.
- `tools/sync_exclude.py` — add/remove/list для `exclude-dirs`,
  с опциональным рестартом демона и локальным сканом.
- `tools/sync_common.py` — общий код для sync‑утилит (демон, локальный скан, snapshot).

Документация: `tasks/sync_manager/simple_sync/sync_tree.md`,
`tasks/sync_manager/simple_sync/exclude.md`.

JSON‑контракт:
- `sync_tree` → `"schema": "sync_tree:v1"`
- `sync_exclude` → `"schema": "sync_exclude:v1"`

Поведение по умолчанию:
- `sync_tree` запускает локальный скан и считает `sync_percent`.
- `sync_exclude --apply` перезапускает демон и запускает локальный скан.

Ключевые поля JSON:
- `sync_tree`: `root.sync_percent`, `local_scan_started`, `local_scan_error`
- `sync_exclude`: `daemon`, `local_scan_started`, `local_scan_error`

### Принципы работы
*   **Offline-first:** Большинство команд работают с локальной базой данных (быстро).
*   **On-demand Scan:** Обращение к API и полное сканирование происходят только по явной команде.
*   **Историчность:** Новые снимки не затирают старые. Каждая запись имеет `scan_id` и `timestamp`.

---

## 2. Структура Базы Данных (SQLite)

### Таблица `scans` (История сканирований)
*   `id` (PK)
*   `timestamp` (Время запуска)
*   `type` ('cloud_meta', 'cloud_full', 'local') — тип сканирования
*   `status` ('success', 'failed')

### Таблица `disk_info` (Метаданные диска)
*   `scan_id` (FK)
*   `total_space` (Всего места)
*   `used_space` (Занято)
*   `trash_size` (Размер корзины)

### Таблица `scan_progress` (Отслеживание прогресса)
*   `scan_id` (FK)
*   `path` (Путь к папке)
*   `status` ('pending', 'in_progress', 'completed')
*   `offset` (Последний обработанный offset для API пагинации)
*   `total_items` (Всего элементов в папке)
*   `last_checked` (Время последней проверки)

---

## 3. Resumable сканирование (Cloud Scanner)

### Жизненный цикл tmpfs БД
*   **Создание**: Каждый старт сканирования создает СВЕЖУЮ tmpfs БД (старые данные отбрасываются)
*   **Использование**: Все операции выполняются в RAM (/dev/shm) для максимальной скорости
*   **Checkpoint**: Периодически (каждые 100 папок/10K файлов/5 минут) копируются на диск
*   **Очистка**: При прерывании (Ctrl+C), ошибке или успешном завершении tmpfs БД удаляется
    - Все данные уже синхронизированы на диск через checkpoints
    - tmpfs остается чистым для следующего сканирования
*   **Восстановление**: При resume - данные берутся из disk DB, а не из старого tmpfs

### Как это работает
*   Каждая папка в облаке имеет запись в `scan_progress` с статусом и offset
*   При прерывании сканирования (Ctrl+C) все состояние сохранено в БД
*   При возобновлении все папки восстанавливаются, и сканирование продолжается с последнего offset
*   **Один scan_id** для всех попыток одного логического сеанса сканирования

### Checkpoint система (Двухуровневое хранилище)

#### Архитектура
Утилита использует **двухуровневое хранилище** для безопасности и производительности:

1. **RAM DB (tmpfs at /dev/shm):** Горячее хранилище
   - Все файлы накапливаются в памяти во время сканирования
   - Ноль операций fsync (максимальная скорость)
   - Теряется при перезагрузке системы (intentional design)
   
2. **Disk DB (monitor.db):** Персистентное хранилище
   - Получает incremental checkpoints из RAM DB
   - Сохраняет все важные данные и метаинформацию
   - Выживает при перезагрузке и сбоях

#### Механизм checkpointing

**Когда происходит checkpoint:**
- **Автоматический (по условиям):** После 10K файлов ИЛИ 5 минут (configurable)
- **Принудительный (на прерывание):** SIGTERM/SIGUSR1 триггерит graceful flush
- **Финальный (на завершение):** После успешного сканирования

**Что происходит при checkpoint:**
1. Flush pending batch из RAM
2. SELECT данные из RAM DB (без ID, чтобы избежать PRIMARY KEY конфликтов)
3. INSERT OR IGNORE в disk DB с auto-increment IDs
4. Отметить в scan_progress статусы завершенных папок
5. Сохранить timestamp последнего checkpoint

**Пример:** Scan 40 (тест на /Books):
```
Запуск: 70 сек сканирования → SIGTERM
├─ Flush batch 2590 файлов/183 папок на диск
├─ Checkpoint завершен успешно
└─ tmpfs очищен, процесс завершен

Resume: --resume на том же scan_id
├─ Восстановление статуса папок из disk DB
├─ Продолжение с pending папок
├─ Еще 14480 файлов/208 папок добавлено на диск
└─ Итог: 14480 файлов persisted успешно
```

**Преимущества:**
- ✅ Данные не теряются при SIGTERM (graceful flush)
- ✅ Нет PRIMARY KEY конфликтов (правильный SELECT)
- ✅ RAM остается быстрым (no fsync)
- ✅ Диск содержит все важное (incremental checkpoints)

#### Конфигурируемые пороги (Config Profiles)

Утилита поддерживает профили конфигурации для гибкой настройки checkpointing:

**prod (production)** — стабильная работа:
```json
{
  "batch_size": 500,
  "checkpoint_files_threshold": 10000,
  "checkpoint_time_sec": 300
}
```

**test (testing)** — быстрое тестирование:
```json
{
  "batch_size": 100,
  "checkpoint_files_threshold": 500,
  "checkpoint_time_sec": 30
}
```

Использование:
```bash
# Production (default)
python3 ydm.py scan cloud --progress

# Testing (быстрые checkpoints для разработки)
python3 ydm.py --config-profile test scan cloud --progress
```

### Этапы сканирования папки
1. `pending` → папка добавлена в очередь, но не начинала сканироваться
2. `in_progress` → сейчас сканируем, offset показывает где остановились
3. `completed` → полностью отсканирована

### Стратегия tmpfs БД
**Зачем именно так?**
- **Всегда свежая БД**: Предотвращает смешивание старых и новых данных при resume
- **Гарантированная очистка**: tmpfs остается чистым, не накапливается мусор
  - Очищается при: успешном завершении, Ctrl+C, ошибке, или при выходе (atexit)
  - Не очищается только при SIGKILL (-9), что нормально
- **Безопасность**: Все важные данные на диске через checkpoints, tmpfs - только кэш
- **Производительность**: RAM работает максимально быстро, диск нагружается только checkpoints
- **Прозрачность**: Всегда ясно какие данные откуда берутся (tmpfs всегда = текущий скан)

### Graceful Interruption & Recovery (Обработка SIGTERM/SIGUSR1)

#### Проблема, которую мы решили
При прерывании сканирования (Ctrl+C или SIGTERM) данные из RAM DB теряли бы в старой реализации, так как батчи накапливались в памяти, но никогда не записывались на диск.

**Решение:** Флаг-based graceful shutdown с принудительным flush
```python
# Signal handler (lines 33-56)
_terminate_requested = False

def signal_handler(signum, frame):
    global _terminate_requested
    _terminate_requested = True  # NOT sys.exit()! Just set flag.
    # Scan loop checks flag and flushes batch before raising TerminationRequested
```

#### Механизм Graceful Shutdown

**Шаг 1: Получение сигнала**
```bash
kill -TERM $SCAN_PID  # или Ctrl+C
```

**Шаг 2: Установка флага (не выход)**
- Signal handler устанавливает `_terminate_requested = True`
- Не вызывает `sys.exit()` (опасно, потеря данных)
- Позволяет scan loop завершить текущую итерацию

**Шаг 3: Проверка в scan loop**
```python
# CloudScanner.scan() (lines 831-938)
while scanning:
    if is_termination_requested():
        flush_and_terminate()  # Flush batch → checkpoint → raise TerminationRequested
```

**Шаг 4: Flush & Checkpoint**
```python
def flush_and_terminate():
    storage.save_files_batch()        # Записать pending файлы в RAM DB
    storage.checkpoint_to_disk()      # Скопировать на диск
    storage.cleanup_temp_db()         # Очистить tmpfs
    raise TerminationRequested()      # Signal safe exit
```

**Шаг 5: Graceful exit**
- Весь код выполнен, данные на диске
- `TerminationRequested` поймана в main
- Выводится JSON сообщение о graceful shutdown
- Процесс завершается с кодом 0

#### Пример: Scan 40 Interrupt/Resume Test

**Начало:**
```bash
$ python3 ydm.py scan cloud --path /Books --progress
{"status": "progress", "scanned": 100, "current": "/Books/Fiction", ...}
{"status": "progress", "scanned": 500, "current": "/Books/Classics", ...}
...
{"status": "progress", "scanned": 2590, "current": "/Books/Drama", ...}

# [Ждем 70 секунд]
# Ctrl+C (SIGTERM)
```

**Результат прерывания:**
```
Received SIGTERM. Graceful shutdown requested...
Flushing pending batch: 2590 files
Checkpointing to disk...
Cleaning up tmpfs...
{"status": "interrupted", "scan_id": 40, "files_saved": 2590, "folders_saved": 183}
```

**Проверка в БД:**
```sql
SELECT COUNT(*) FROM files WHERE scan_id = 40;
-- 2590 файлов успешно сохранено

SELECT COUNT(*) FROM scan_progress WHERE scan_id = 40 AND status = 'pending';
-- 200 папок еще в очереди (resumable!)
```

**Возобновление:**
```bash
$ python3 ydm.py scan cloud --resume --scan-id 40 --progress
{"status": "progress", "scanned": 2590, "current": "/Books/Poetry", ...}
{"status": "progress", "scanned": 3000, "current": "/Books/Reference", ...}
...
{"status": "progress", "scanned": 14480, "current": "/Books/Modern", ...}
{"status": "success", "scan_id": 40, "files_total": 14480, "folders_total": 208}
```

**Финальная проверка:**
```sql
SELECT COUNT(*) FROM files WHERE scan_id = 40;
-- 14480 файлов persisted

SELECT COUNT(*) FROM scan_progress WHERE scan_id = 40 AND status = 'completed';
-- 208 папок завершено
```

**Ключевой вывод:** Все 14480 файлов благополучно дошли на диск через два этапа: первый interrupt + checkpoint (2590 файлов), второй resume (14480 файлов всего).

#### Гарантии безопасности

| Сценарий | Результат | Статус в БД |
|----------|-----------|------------|
| Ctrl+C во время сканирования | Graceful flush → checkpoint | `interrupted`, данные спасены |
| SIGTERM из systemd | Graceful flush → checkpoint | `crashed` (можно resume) |
| Полное сканирование успешно | Финальный checkpoint | `success` |
| Ошибка в API | Graceful flush → checkpoint | `failed`, данные спасены |
| Перезагрузка системы | tmpfs теряется, disk DB сохранен | `crashed`, resumable с last checkpoint |

#### Когда используется какой профиль

**prod (default):** Стабильная работа на production
- `batch_size: 500` — Большие батчи (меньше переключений)
- `checkpoint_files_threshold: 10000` — Редкие checkpoints (меньше нагрузки на диск)
- `checkpoint_time_sec: 300` — 5 минут между checkpoints (баланс безопасности)

**test:** Быстрое тестирование функционала
- `batch_size: 100` — Маленькие батчи (частая проверка логики)
- `checkpoint_files_threshold: 500` — Частые checkpoints (быстрая проверка)
- `checkpoint_time_sec: 30` — 30 сек между checkpoints (не надо ждать 5 минут в тестах)


- Переиспользовать старую tmpfs БД при resume (смешаются данные)
- Оставлять tmpfs БД на диск при завершении (дублирование)
- Игнорировать Ctrl+C (потеря данных)

## Быстрые команды

**Важно:** Каждая команда должна выполняться отдельно. Не склеивайте команды вместе - это приведет к ошибке. Если нужно выполнить несколько команд подряд, используйте `&&` или `;`.

### Production (стабильная работа - по умолчанию)
```bash
# Начать новое сканирование с корня (prod профиль: checkpoints каждые 10K файлов или 5 минут)
python3 ydm.py scan cloud --progress 2>&1 | tee /tmp/scan.log &

# Продолжить прерванный скан (graceful shutdown гарантирует спасение данных)
python3 ydm.py scan cloud --resume --progress 2>&1 | tee /tmp/scan_resume.log &

# Сканировать конкретную папку
python3 ydm.py scan cloud --path "/Архив" --progress

# Безопасное прерывание (Ctrl+C или kill -TERM)
# Данные автоматически сохранятся на диск перед выходом
# 
# Пример безопасного прерывания:
# SCAN_PID=$(ps aux | grep "python3 ydm.py scan cloud" | grep -v grep | awk '{print $2}')
# kill -TERM $SCAN_PID  # Graceful shutdown с сохранением checkpoint
```

### Test/Development (быстрая разработка и тестирование)
```bash
# Запуск с test профилем (checkpoints каждые 30 сек или 500 файлов)
python3 ydm.py --config-profile test scan cloud --path /Books --progress

# Быстрый цикл interrupt/resume для тестирования
python3 ydm.py --config-profile test scan cloud --progress &
sleep 10
kill -TERM $!  # Graceful shutdown
python3 ydm.py --config-profile test scan cloud --resume --progress
```

### Мониторинг и статус
```bash
# Проверить статус всех сканов (JSON для парсинга)
python3 ydm.py --format json report scan-list --limit 10

# Детали конкретного скана (ID=42)
python3 ydm.py --format json report scan-info --scan-id 42

# Прогресс сеанса (сколько папок осталось)
python3 ydm.py --format json report scan-progress --scan-id 42 | jq '.data.scan.progress'

# Мониторить в реальном времени (пока сканирование работает)
tail -f /tmp/scan.log | jq -r 'select(.status=="progress") | "\(.scanned) files - \(.current)"'
```

### Продвинутые сценарии
```bash
# Возобновить конкретный скан (с явным ID)
python3 ydm.py scan cloud --resume --scan-id 42 --progress

# Добавить несколько папок в один сеанс сканирования
python3 ydm.py scan cloud --path "/Фото" --add-to-scan 42
python3 ydm.py scan cloud --path "/Видео" --add-to-scan 42
python3 ydm.py scan cloud --path "/Документы" --add-to-scan 42

# Потом запустить сканирование этих папок вместе
python3 ydm.py scan cloud --resume --scan-id 42 --progress

# Проверить целостность данных на диске
ls -lh monitor.db  # Должен быть 50+ MB (хранит историю)
ls /dev/shm/ydm_scan.db 2>&1  # Может не существовать (это OK - tmpfs чистый)
```

## Быстрая диагностика состояния

### Основные проверки по scan_id
```bash
# Статус, количество файлов и длительность (JSON)
python3 ydm.py --format json report scan-info --scan-id 41 | jq '.data | {status, files_count, duration}'

# Прогресс по папкам (completed/pending) для оценки остатка работы
python3 ydm.py --format json report scan-progress --scan-id 41 | jq '.data.scan.progress'

# Подсчет сколько файлов vs папок записано в БД
sqlite3 monitor.db "SELECT SUM(type='file') AS files, SUM(type='dir') AS dirs FROM files WHERE scan_id=41;"

# Проверка наличия файлов (не только папок)
sqlite3 monitor.db "SELECT COUNT(*) FROM files WHERE scan_id=41 AND type='file';"
```

### Быстрые выборки примеров файлов
```bash
# Показать несколько файлов с путями и размером
sqlite3 monitor.db "SELECT parent_path || '/' || name AS path, size, md5 FROM files WHERE scan_id=41 AND type='file' LIMIT 20;"

# Выборка файлов из конкретной папки (пример для /Books)
sqlite3 monitor.db "SELECT parent_path || '/' || name FROM files WHERE scan_id=41 AND type='file' AND parent_path LIKE '/Books%' LIMIT 20;"
```

### Интегральные проверки целостности
```bash
# Общий статус последнего скана (JSON)
python3 ydm.py --format json report scan-list --limit 1

# Проверка размера дисковой БД и отсутствия tmpfs (нормально, если отсутствует)
ls -lh monitor.db
ls /dev/shm/ydm_scan.db 2>&1
```

### Как устроено возобновление логически

В момент SIGTERM/ctrl‑C: текущий батч пишется в RAM → делается checkpoint в monitor.db → tmpfs очищается → статус скана остаётся тем же (после рестарта помечаем crashed/interrupted).

При --resume:
- tmpfs заново создаётся;
- все строки scan_progress + files для данного scan_id копируются с диска в RAM;
- для каждой папки берётся её offset/статус из scan_progress;
- обход продолжается только для pending/in_progress папок с их последними offset.

Дубликаты не допускаются: UNIQUE (scan_id, parent_path, name) + INSERT OR IGNORE, поэтому повторный заход в ту же папку не испортит данные, просто проигнорирует уже сохранённые файлы.

### Что означает “с места прерывания” practically

offset в scan_progress хранит последний обработанный элемент пагинации для каждой папки; при resume запрос к API стартует с этого offset, а уже записанные файлы по той папке либо игнорируются (UNIQUE), либо не запрашиваются, если offset уже продвинут.

### Как диагностировать, что ничего не потеряли и не пропускаем

- Сравнить счётчики файлов до/после возобновления:
    - До: `sqlite3 monitor.db "SELECT COUNT(*) FROM files WHERE scan_id=41;"`
    - После часа работы resume: повторить и убедиться, что число растёт (монотонно).
- Проверить статус и прогресс:
    - `python3 ydm.py --format json report scan-info --scan-id 41 | jq '.data | {status, files_count, duration}'`
    - `python3 ydm.py --format json report scan-progress --scan-id 41 | jq '.data.scan.progress'`
    Убеждаемся, что pending уменьшается, completed растёт.
- Убедиться, что есть файлы, а не только папки:
    - `sqlite3 monitor.db "SELECT SUM(type='file') AS files, SUM(type='dir') AS dirs FROM files WHERE scan_id=41;"`
- Посмотреть выборку реальных файлов (знаем, что они пишутся):
    - `sqlite3 monitor.db "SELECT parent_path || '/' || name AS path, size FROM files WHERE scan_id=41 AND type='file' LIMIT 20;"`
- Проверить, что offset по конкретной папке двигается:
    - `sqlite3 monitor.db "SELECT path, status, offset, total_items FROM scan_progress WHERE scan_id=41 AND path LIKE '/Books%' LIMIT 10;"`
    Если offset > 0 и status in_progress/completed, значит внутри папки шли запросы и сдвиг пагинации.
- Контроль дубликатов/пропусков:
    - UNIQUE гарантирует отсутствие дублей; пропуск невозможен, т.к. offset задаёт следующий элемент, а уже записанные элементы при повторном запросе будут либо не запрошены (offset), либо проигнорированы (UNIQUE).

### Практический чеклист после resume

- files_count растёт с течением времени.
- pending ↓, completed ↑ в scan_progress.
- В выборке файлов есть новые пути, в том числе из папки, где прервали.
- offset по “прерванной” папке > 0 или статус уже completed.
- tmpfs отсутствует/маленький после graceful остановки: `ls /dev/shm/ydm_scan.db 2>&1` (для уверенности, что работаем с дисковым чекпоинтом).

---

## 4. Workflow: Пайплайн сканирования облака

### Для AI-ассистентов и автоматизации

Этот раздел описывает полный цикл работы с утилитой при сканировании облака. Все команды возвращают JSON для удобного парсинга.

### 🎯 Quick Start для AI

**Минимальный набор команд для типичного сценария:**
```bash
# 1. Проверить статус
python3 ydm.py --format json report scan-list --limit 5

# 2. Если есть прерванный скан - продолжить
python3 ydm.py scan cloud --resume --progress 2>&1 | tee /tmp/scan.log &

# 3. Мониторить прогресс
tail -f /tmp/scan.log | jq -r 'select(.status=="progress") | "\(.scanned) files"'

# 4. Проверить в другом терминале
python3 ydm.py --format json report scan-progress --scan-id <ID> | jq '.data.scan.progress'
```

**Ключевые принципы:**
- ✅ Всегда использовать `--format json` для report команд
- ✅ Всегда использовать `--progress` для scan команд
- ✅ Запускать длительные сканы в фоне с `&`
- ✅ Логировать вывод через `tee` для анализа
- ✅ Использовать `jq` для парсинга JSON
- ❌ Не использовать `timeout` для полных сканов (могут идти часами)

#### Шаг 1: Проверка состояния БД и сеансов

```bash
# Проверить последние сеансы (всегда используем --format json)
python3 ydm.py --format json report scan-list --limit 10

# Результат: список сеансов с ID, типом, статусом, временем
# {
#   "success": true,
#   "data": {
#     "total": 5,
#     "scans": [
#       {"id": 5, "timestamp": "2026-01-03 06:59:23", "type": "cloud", "status": "interrupted", ...},
#       {"id": 4, "timestamp": "2026-01-03 05:30:00", "type": "cloud", "status": "success", ...}
#     ]
#   }
# }

# Извлечь только ID и статус последних cloud сканов
python3 ydm.py --format json report scan-list --limit 10 | \
  jq '.data.scans[] | select(.type=="cloud") | {id, status, timestamp}'
```

#### Шаг 2: Определение стратегии сканирования

**Сценарий A: Есть прерванный скан (status = "interrupted" или "started")**
```bash
# Получить детали прерванного сеанса
python3 ydm.py --format json report scan-info --scan-id 5

# Результат показывает прогресс:
# "progress": {
#   "total_folders": 2865,
#   "completed": 1200,
#   "in_progress": 10,
#   "pending": 1655
# }

# Если есть незавершенные папки (pending > 0) - можно продолжить
python3 ydm.py --format json report scan-progress --scan-id 5 | head -50

# Показывает список pending папок - понимаем что осталось сканировать
```

**Сценарий B: Новое сканирование**
```bash
# Если все прошлые сеансы завершены (status = "success")
# или нужно полное пересканирование - начать новый
```

#### Шаг 3: Запуск сканирования

**Продолжение прерванного скана:**
```bash
# Всегда используем --progress для мониторинга
# Запускаем в фоне с перенаправлением вывода
python3 ydm.py scan cloud --resume --scan-id 5 --progress 2>&1 | tee /tmp/scan_5.log &

# Или без --scan-id (возьмет последний cloud scan)
python3 ydm.py scan cloud --resume --progress 2>&1 | tee /tmp/scan_resume.log &
```

**Новое полное сканирование:**
```bash
# Новый скан с корня облака
python3 ydm.py scan cloud --progress 2>&1 | tee /tmp/scan_new.log &

# Или только конкретной папки
python3 ydm.py scan cloud --path "/Архив" --progress 2>&1 | tee /tmp/scan_archive.log &
```

**Ограничения и рекомендации:**
- ⏱️ **Не использовать timeout** для длительных сканов (может занять часы)
- 🔄 **Всегда использовать --progress** для мониторинга
- 📝 **Логировать вывод** через `tee` для последующего анализа
- 🎯 **Запускать в фоне (&)** для долгих операций

#### Шаг 4: Мониторинг процесса

**Отслеживание прогресса (парсинг JSON-строк):**
```bash
# Прогресс выводится построчно в формате JSON
# {"status": "progress", "scanned": 50, "current": "/Books/Classic", "stats": {...}}

# Чтение последней строки лога
tail -1 /tmp/scan_5.log

# Мониторинг в реальном времени
tail -f /tmp/scan_5.log | grep --line-buffered '"status"'

# Извлечь только текущую папку и количество
tail -f /tmp/scan_5.log | jq -r 'select(.status=="progress") | "\(.scanned) files, current: \(.current)"'
```

**Проверка статуса через БД (пока процесс работает):**
```bash
# В другом терминале проверить прогресс через БД
python3 ydm.py --format json report scan-progress --scan-id 5 | jq '.data.scan.progress'

# Результат:
# {
#   "total_folders": 2865,
#   "completed": 1450,    ← растет во время сканирования
#   "in_progress": 5,
#   "pending": 1410       ← уменьшается
# }
```

**Проверка что процесс жив:**
```bash
# Проверить PID процесса
ps aux | grep "python3 ydm.py scan cloud" | grep -v grep

# Если процесс умер - в логе будет финальное сообщение или ошибка
```

#### Шаг 5: Обработка прерывания

**Graceful остановка (Ctrl+C):**
```bash
# Найти PID процесса
SCAN_PID=$(ps aux | grep "python3 ydm.py scan cloud" | grep -v grep | awk '{print $2}')

# Послать SIGINT (как Ctrl+C)
kill -INT $SCAN_PID

# Процесс:
# 1. Получает сигнал
# 2. Делает checkpoint на диск
# 3. Сохраняет статус "interrupted"
# 4. Очищает tmpfs
# 5. Выводит финальное JSON сообщение

# Проверить что сохранилось
python3 ydm.py --format json report scan-info --scan-id 5
```

#### Шаг 6: Проверка результата

**После завершения (успешного или прерванного):**
```bash
# Получить детальную статистику
python3 ydm.py --format json report scan-info --scan-id 5

# Ключевые поля для AI-парсинга:
# {
#   "id": 5,
#   "status": "success" | "interrupted" | "failed",
#   "files_count": 144089,              ← сколько файлов отсканировано
#   "total_size_bytes": 1810019500335,  ← общий размер
#   "duration": 3654.5,                 ← время в секундах
#   "progress": {
#     "completed": 2865,                ← завершено папок
#     "pending": 0                      ← осталось папок (0 = готово)
#   }
# }
```

**Проверка целостности данных:**
```bash
# Убедиться что данные на диске
ls -lh monitor.db
# Должна быть нормального размера (десятки MB)

# Убедиться что tmpfs чистый
ls /dev/shm/ydm_scan.db 2>&1
# Должно быть "Нет такого файла" или очень маленький размер
```

### Полный пример скрипта для AI-ассистента

```bash
#!/bin/bash
# Автоматический workflow сканирования

LOG_DIR="/tmp/ydm_scans"
mkdir -p "$LOG_DIR"

# 1. Проверка статуса
echo "=== Checking scan status ==="
SCANS=$(python3 ydm.py --format json report scan-list --limit 5)
echo "$SCANS" | jq '.data.scans[] | {id, type, status, timestamp}'

# 2. Найти последний cloud scan
LAST_CLOUD=$(echo "$SCANS" | jq -r '.data.scans[] | select(.type=="cloud") | .id' | head -1)
LAST_STATUS=$(echo "$SCANS" | jq -r ".data.scans[] | select(.id==$LAST_CLOUD) | .status")

echo "Last cloud scan: ID=$LAST_CLOUD, status=$LAST_STATUS"

# 3. Определить действие
if [[ "$LAST_STATUS" == "interrupted" ]] || [[ "$LAST_STATUS" == "started" ]]; then
    # Проверить есть ли pending работа
    PENDING=$(python3 ydm.py --format json report scan-progress --scan-id "$LAST_CLOUD" | jq -r '.data.scan.progress.pending')
    
    if [[ "$PENDING" -gt 0 ]]; then
        echo "=== Resuming scan $LAST_CLOUD (pending: $PENDING folders) ==="
        python3 ydm.py scan cloud --resume --scan-id "$LAST_CLOUD" --progress 2>&1 | tee "$LOG_DIR/scan_${LAST_CLOUD}_resume.log" &
        SCAN_PID=$!
    else
        echo "=== Scan $LAST_CLOUD already completed, starting new ==="
        python3 ydm.py scan cloud --progress 2>&1 | tee "$LOG_DIR/scan_new.log" &
        SCAN_PID=$!
    fi
else
    echo "=== Starting new cloud scan ==="
    python3 ydm.py scan cloud --progress 2>&1 | tee "$LOG_DIR/scan_new.log" &
    SCAN_PID=$!
fi

# 4. Мониторинг (первые 60 секунд)
echo "=== Monitoring process (PID=$SCAN_PID) for 60 seconds ==="
for i in {1..12}; do
    sleep 5
    if ps -p $SCAN_PID > /dev/null; then
        # Показать последний прогресс
        tail -1 "$LOG_DIR"/*.log | grep -o '"scanned":[0-9]*' || echo "Running..."
    else
        echo "Process finished"
        break
    fi
done

echo "=== Scan running in background. Monitor with: ==="
echo "tail -f $LOG_DIR/*.log"
echo "ps aux | grep 'python3 ydm.py'"
```

### Типичные паттерны для AI

**Проверка завершенности сканирования:**
```bash
STATUS=$(python3 ydm.py --format json report scan-info --scan-id 5 | jq -r '.data.status')
PENDING=$(python3 ydm.py --format json report scan-progress --scan-id 5 | jq -r '.data.scan.progress.pending')

if [[ "$STATUS" == "success" ]] && [[ "$PENDING" == "0" ]]; then
    echo "✓ Scan fully completed"
elif [[ "$STATUS" == "interrupted" ]] && [[ "$PENDING" -gt 0 ]]; then
    echo "⚠ Scan interrupted, can resume"
else
    echo "ℹ Scan in progress or failed"
fi
```

**Estimate времени завершения (грубая оценка):**
```bash
INFO=$(python3 ydm.py --format json report scan-info --scan-id 5)
COMPLETED=$(echo "$INFO" | jq -r '.data.progress.completed')
PENDING=$(echo "$INFO" | jq -r '.data.progress.pending')
DURATION=$(echo "$INFO" | jq -r '.data.duration')

if [[ "$COMPLETED" -gt 0 ]]; then
    AVG_TIME=$(echo "scale=2; $DURATION / $COMPLETED" | bc)
    ETA=$(echo "scale=0; $AVG_TIME * $PENDING" | bc)
    echo "ETA: ~${ETA} seconds ($(echo "scale=0; $ETA / 60" | bc) minutes)"
fi
```

---

## 5. Синтаксис команд и флаги

### Общий синтаксис
```bash
python3 ydm.py [ГЛОБАЛЬНЫЕ_ФЛАГИ] КОМАНДА [ФЛАГИ_КОМАНДЫ]
```

### Глобальные флаги (перед командой)
- `--db-path PATH` — Путь к базе данных SQLite (по умолчанию: `monitor.db`)
- `--format {text|json}` — Формат **вывода результата** в консоль (по умолчанию: `text`)
  - **Важно:** Влияет только на **вывод в терминал**, НЕ на сохранение в БД!
  - `text` — человекочитаемый формат
  - `json` — для автоматизации и парсинга
- `-h, --help` — Показать справку

### Команды

#### `init` — Инициализация базы данных
```bash
python3 ydm.py init
```
Создаёт таблицы и индексы в БД.

#### `scan` — Сканирование
```bash
python3 ydm.py scan {meta|local|cloud} [ФЛАГИ]
```

**Типы сканирования:**
- `meta` — Быстрая метаинформация (место на диске)
- `local` — Сканирование локальной файловой системы
- `cloud` — Полное сканирование облака (resumable)

**Флаги команды scan:**
- `--path PATH` — Путь для сканирования
  - Для `cloud`: `/ПапкаВОблаке`
  - Для `local`: `/путь/на/диске` (по умолчанию: `/data/ya_disk`)
- `--progress` — Показывать прогресс в виде JSON-строк (для мониторинга)
- `--resume` — Продолжить прерванное сканирование
- `--scan-id ID` — ID конкретного сеанса для продолжения
- `--add-to-scan ID` — Добавить `--path` в существующий сеанс (не создавая новый)

#### `report` — Отчёты и аналитика
```bash
python3 ydm.py report {TYPE} [ФЛАГИ]
```

**Типы отчётов:**
- `status` — Последние 5 сканирований
- `diff` — Сравнение cloud vs local
- `scan-list` — Список всех сеансов
- `scan-info` — Детали конкретного сеанса
- `scan-progress` — Прогресс сеанса с папками

**Флаги команды report:**
- `--scan-id ID` — ID сеанса (для `scan-info` и `scan-progress`)
- `--limit N` — Лимит записей (для `scan-list`, по умолчанию: 20)

### Примеры с флагами
```bash
# Глобальные флаги ПЕРЕД командой
python3 ydm.py --db-path /tmp/test.db --format json scan cloud --progress

# Вывод в JSON (для парсинга скриптами)
python3 ydm.py --format json report scan-list --limit 10

# Использование другой БД
python3 ydm.py --db-path ~/backups/monitor.db report status

# Справка по команде
python3 ydm.py scan --help
python3 ydm.py report --help
```

---

## 5. Режимы работы (Команды)

---

## 6. Режимы работы (Команды)

### А. Сбор данных
1.  **`scan meta`**
    *   Быстрый запрос к `v1/disk/`.
    *   Сохраняет: Общий объем, занято, свободно.
    *   Использование: Можно запускать часто (хоть по cron раз в час).
    ```bash
    python3 ydm.py scan meta
    ```
2.  **`scan cloud`**
    *   Долгий рекурсивный обход `v1/disk/resources`.
    *   Сохраняет: Дерево всех файлов и папок.
    *   **Resumable:** Можно прервать (Ctrl+C) и продолжить с того же места.
    *   **Избирательное сканирование:** Можно сканировать конкретные папки.
    ```bash
    # Полное сканирование с корня
    python3 ydm.py scan cloud [--progress] [--format json]
    
    # Новый сеанс только для одной папки
    python3 ydm.py scan cloud --path "/Архив"
    
    # Добавить папку в существующий сеанс
    python3 ydm.py scan cloud --path "/Документы" --add-to-scan 6
    
    # Продолжить прерванный скан
    python3 ydm.py scan cloud --resume [--scan-id 5]
    ```
3.  **`scan local`**
    *   Быстрый обход папки на локальном диске.
    *   Сохраняет: Реальное состояние на диске.
    ```bash
    # Сканировать по умолчанию /data/ya_disk
    python3 ydm.py scan local
    
    # Сканировать конкретную папку
    python3 ydm.py scan local --path /home/user/Documents
    ```

### Б. Аналитика (Работа с БД)

#### Основные отчёты
1.  **`report status`**
    *   Показывает последние 5 сканирований из БД.
    ```bash
    python3 ydm.py report status
    ```

2.  **`report diff`**
    *   Сравнивает последний снимок `cloud` с `local`.
    *   Учитывает `exclude-dirs` из конфига.
    *   Выявляет файлы, которые:
        *   Есть в облаке, но не скачаны (и не исключены) → Ошибка синхронизации.
        *   Есть локально, но нет в облаке → Не загружено.
        *   Различаются по размеру → В процессе / Битый файл.
    ```bash
    python3 ydm.py report diff
    ```

#### Мониторинг сеансов сканирования

3.  **`report scan-list`**
    *   Показывает список всех сеансов сканирования.
    *   По умолчанию последние 20 сеансов.
    ```bash
    python3 ydm.py report scan-list [--limit 50]
    ```

4.  **`report scan-info`**
    *   Детальная информация о конкретном сеансе.
    *   Показывает статус, количество файлов, размер, прогресс.
    ```bash
    python3 ydm.py report scan-info --scan-id 42
    ```

5.  **`report scan-progress`**
    *   Подробный статус сеанса и список папок в очереди.
    *   Полезно для отслеживания resumable сканирования.
    ```bash
    python3 ydm.py report scan-progress --scan-id 42
    ```

---

## 7. Технический стек
*   **Язык:** Python 3 (без внешних зависимостей типа pandas/requests, только stdlib).
*   **БД:** `sqlite3`.
*   **Конфиг:** Чтение токена из `.env` и исключений из `config.cfg`.

## 8. План реализации
1.  [x] Создать структуру БД (`db.py` - init).
2.  [x] Реализовать `scan meta` (API `v1/disk`).
3.  [x] Реализовать `scan cloud` (Рекурсия `v1/disk/resources`).
4.  [x] Реализовать `scan local` (`os.walk`).
5.  [x] Реализовать команду `report` (вывод данных из БД).
6.  [x] Optimized DB writes (tmpfs для сканирования).
7.  [x] Resumable cloud scan с checkpointing.
8.  [x] AI-friendly workflow и automation pipeline.
