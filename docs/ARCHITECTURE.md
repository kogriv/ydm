# Архитектура Yandex Disk Monitor (YDM)

**Цель:** Создать модульную, расширяемую CLI-утилиту для аудита Яндекс.Диска с поддержкой машиночитаемого вывода (JSON) для интеграции с AI-агентами.

---

## 1. Концепция

Утилита строится по принципу разделения ответственности (SoC). Интерфейс (CLI) отделен от бизнес-логики и работы с данными.

### Основные слои:
1.  **View (CLI):** Обработка аргументов, форматирование вывода (Text/JSON/Table).
2.  **Service (Collector/Analyzer):** Логика сбора данных (API/Disk) и их анализа (Diff).
3.  **Model (Storage):** Абстракция над базой данных SQLite.

---

## 2. Проектируемый CLI Интерфейс

### Общие флаги
*   `--format [text|json]`: Формат вывода. По умолчанию `text`. `json` критичен для автоматизации и AI.
*   `--db-path FILE`: Путь к БД (по умолчанию `./monitor.db`).

### Команды

#### A. Управление (Admin)
*   `init`: Инициализация базы данных (создание таблиц).
*   `clean`: Очистка старых сканов (например, старше 30 дней).

#### B. Сканирование (Collector)
Команда `scan` запускает процесс сбора данных и сохраняет результат в БД.
*   `scan meta`: Быстрый запрос квоты диска (Total/Used/Trash).
*   `scan cloud`: Полное рекурсивное сканирование облака через API.
*   `scan local`: Сканирование локальной директории синхронизации.

#### C. Отчеты (Reader)
Команда `report` читает данные из БД (офлайн-режим).
*   `report status`: Сводка (последние сканы, текущее место).
*   `report history`: Динамика изменений за период.
*   `report tree --scan-id ID`: Просмотр файловой структуры конкретного снимка.

#### D. Анализ (Analyzer)
*   `diff`: Сравнение последнего облачного и локального снимка. Выявляет:
    *   Missing Local (есть в облаке, нет на диске, не в ignore).
    *   Missing Cloud (есть на диске, не залито).
    *   Mismatch (разные размеры).

---

## 3. Модель Базы Данных (SQLite)

### `scans`
Журнал операций сканирования.
```sql
CREATE TABLE scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    scan_type TEXT,      -- 'meta', 'cloud', 'local'
    status TEXT,         -- 'started', 'success', 'failed'
    duration REAL        -- длительность в секундах
);
```

### `disk_info`
Результаты сканирования `meta`.
```sql
CREATE TABLE disk_info (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER,
    total_space INTEGER, -- байт
    used_space INTEGER,  -- байт
    trash_size INTEGER,  -- байт
    FOREIGN KEY(scan_id) REFERENCES scans(id)
);
```

### `files`
Результаты сканирования `cloud` и `local`. Единая таблица для хранения файлового дерева.
```sql
CREATE TABLE files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER,
    parent_path TEXT,    -- путь к родительской папке
    name TEXT,           -- имя файла
    type TEXT,           -- 'file' или 'dir'
    size INTEGER,
    md5 TEXT,            -- может быть NULL для local
    created DATETIME,
    modified DATETIME,
    FOREIGN KEY(scan_id) REFERENCES scans(id)
);
```
*Индекс по `(scan_id, parent_path)` для быстрого поиска.*

---

## 4. Классовая структура (Python)

```python
class StorageManager:
    """Обертка над SQLite. Выполняет CRUD операции."""
    def init_db(self): ...
    def create_scan(self, type): ...
    def save_file(self, scan_id, file_obj): ...

class YandexClient:
    """Работа с REST API Яндекса."""
    def get_disk_info(self): ...
    def walk_resources(self): ...

class LocalScanner:
    """Работа с локальной ФС."""
    def walk_dir(self, path): ...

class CLI:
    """Точка входа. Парсинг argparse и выбор форматтера."""
    def run(self): ...
```

---

## 5. Двухуровневое хранилище (Two-Tier Storage)

### Архитектура

Утилита использует **двухуровневую архитектуру** для балансирования производительности и безопасности:

#### Уровень 1: RAM DB (tmpfs at /dev/shm)
```
/dev/shm/ydm_scan.db
├─ **Назначение:** Горячее хранилище во время сканирования
├─ **Транзакции:** Все операции в памяти, ZERO fsync
├─ **Скорость:** Максимальная (отсутствие I/O)
├─ **Персистентность:** Теряется при reboot (intentional)
├─ **Размер:** Растет с файлами (обычно 10-100 MB)
└─ **Жизненный цикл:** Создается при старте скана, удаляется при завершении
```

#### Уровень 2: Disk DB (monitor.db)
```
monitor.db (основная база, 50+ MB)
├─ **Назначение:** Персистентное хранилище
├─ **Транзакции:** Incremental checkpoints из RAM DB
├─ **Скорость:** Медленнее (диск), но надежнее
├─ **Персистентность:** Выживает при reboot и сбоях
├─ **Размер:** Растет с каждым сканом (исторические данные)
└─ **Восстановление:** Используется для resume при crash
```

### Механизм Checkpointing

**Данные текут в одном направлении:**
```
RAM DB accumulation → Checkpoint decision → Disk DB increment → Clean termination → Resume capability
```

**Checkpointing происходит в следующих случаях:**

1. **По условиям (автоматический):**
   - После 10,000 файлов (configurable: `checkpoint_files_threshold`)
   - ИЛИ после 5 минут (configurable: `checkpoint_time_sec`)
   - ИЛИ после завершения 100 папок (batching logic)

2. **При получении сигнала (graceful flush):**
   - SIGTERM: Установить флаг `_terminate_requested`
   - Scan loop проверяет флаг между итерациями
   - Вызывает `flush_and_terminate()` перед выходом

3. **При финализации:**
   - После успешного завершения всех папок
   - Final checkpoint → mark as 'success' → cleanup tmpfs

**Алгоритм checkpoint:**
```python
def checkpoint_to_disk():
    # 1. Flush pending batch из RAM DB
    save_files_batch()
    
    # 2. SELECT из tmpfs (БЕЗ id column!)
    SELECT scan_id, parent_path, name, type, size, md5, created, modified
    FROM files WHERE scan_id = <current>
    
    # 3. INSERT OR IGNORE в disk DB (БЕЗ id - позволяет auto-increment)
    INSERT OR IGNORE INTO files (...)
    SELECT ... FROM tmpfs_files
    
    # 4. Обновить scan_progress статусы
    UPDATE scan_progress SET status='completed' WHERE scan_id=<current>
    
    # 5. Сохранить timestamp checkpoint
    UPDATE scans SET last_checkpoint=NOW() WHERE id=<current>
```

**Ключевой момент:** Выбор БЕЗ id column исправляет PRIMARY KEY конфликты:
- tmpfs DB имеет id 1,2,3,4...
- disk DB имеет id 1000,1001,1002...
- Если копировать с id → conflict!
- Если копировать БЕЗ id → auto-increment генерирует новые ID

---

## 6. Signal Handling & Graceful Shutdown

### Проблема: Потеря данных при SIGTERM

**Старый подход (неправильный):**
```python
def signal_handler(sig, frame):
    sys.exit(0)  # ❌ Немедленный выход, данные в RAM DB теряются!
```

**Новый подход (правильный):**
```python
_terminate_requested = False

def signal_handler(sig, frame):
    global _terminate_requested
    _terminate_requested = True  # ✅ Только флаг! Позволяет scan loop завершить батч
```

### Поток выполнения при SIGTERM

```
[Signal received: SIGTERM]
         ↓
[signal_handler() sets _terminate_requested = True]
         ↓
[Scan loop checks flag in next iteration]
         ↓
[is_termination_requested() returns True]
         ↓
[flush_and_terminate() called]
         ├─ save_files_batch()         ← Записать pending файлы в RAM DB
         ├─ checkpoint_to_disk()       ← Скопировать на диск
         ├─ cleanup_temp_db()          ← Очистить tmpfs
         └─ raise TerminationRequested ← Signal safe exit
         ↓
[main() catches TerminationRequested]
         ├─ Log final JSON message
         ├─ Mark scan as 'interrupted' (status)
         └─ sys.exit(0) ← Clean exit
```

### Обработка исключений в main()

```python
try:
    # Установить глобальные переменные для signal handler
    _storage_for_signal = storage
    _scan_id_for_signal = scan_id
    _start_time_for_signal = start_time
    
    # Запустить сканирование
    cloud_scanner.scan(...)
    
except TerminationRequested:
    # SIGTERM/SIGUSR1 - данные уже сохранены
    print(json.dumps({
        "status": "interrupted",
        "scan_id": scan_id,
        "files_saved": file_count,
        "checkpoint_successful": True
    }))
    sys.exit(0)

except KeyboardInterrupt:
    # Ctrl+C - тоже graceful, но через signal handler
    sys.exit(1)

except Exception as e:
    # Другие ошибки - emergency checkpoint
    try:
        storage.checkpoint_to_disk()
    except:
        pass
    raise
```

---

## 7. Configuration Profiles (Конфигурируемые профили)

### Файл ydm_config.json

```json
{
  "prod": {
    "batch_size": 500,
    "checkpoint_files_threshold": 10000,
    "checkpoint_time_sec": 300,
    "description": "Production profile - stable operation"
  },
  "test": {
    "batch_size": 100,
    "checkpoint_files_threshold": 500,
    "checkpoint_time_sec": 30,
    "description": "Test profile - rapid feedback"
  }
}
```

### Использование профилей

```bash
# Production (default)
python3 ydm.py scan cloud --progress

# Или explicit
python3 ydm.py --config-profile prod scan cloud --progress

# Test (для разработки)
python3 ydm.py --config-profile test scan cloud --progress
```

### Параметры

| Параметр | prod | test | Назначение |
|----------|------|------|-----------|
| `batch_size` | 500 | 100 | Размер батча перед save_files_batch() |
| `checkpoint_files_threshold` | 10000 | 500 | Checkpoint после N файлов |
| `checkpoint_time_sec` | 300 | 30 | Checkpoint после N секунд |

**Зачем разные профили?**
- **prod:** Редкие checkpoints (меньше нагрузки на диск), большие батчи (меньше переключений контекста)
- **test:** Частые checkpoints (быстрая обратная связь), маленькие батчи (тонкая логирование)

---

## 8. Recovery Mechanism (Восстановление после crash)

### Статусы скана в БД

```sql
-- После успешного завершения
UPDATE scans SET status='success' WHERE id=<ID>;

-- После graceful interruption (SIGTERM)
UPDATE scans SET status='interrupted' WHERE id=<ID>;

-- После crash (если tmpfs существует при старте)
UPDATE scans SET status='crashed' WHERE id=<ID>;

-- При начале нового скана
UPDATE scans SET status='started' WHERE id=<ID>;
```

### Как обнаруживается crash?

```python
def is_crash_recovery_needed():
    """Проверить существует ли tmpfs DB от прошлого скана"""
    if os.path.exists(TMPFS_DB_PATH):
        # tmpfs существует → значит был crash (SIGKILL или reboot)
        return True
    return False

def mark_previous_as_crashed():
    """Отметить предыдущий скан как crashed"""
    storage.update_scan_status(previous_scan_id, 'crashed')
```

### Процесс resume

```bash
# 1. Проверить какие папки остались в статусе pending
SELECT * FROM scan_progress WHERE scan_id=<ID> AND status='pending'

# 2. Установить их в 'in_progress'
UPDATE scan_progress SET status='in_progress' WHERE scan_id=<ID> AND status='pending'

# 3. Для каждой папки - продолжить с offset
offset = scan_progress.offset  # Где остановились
response = api.list(path, offset=offset)

# 4. Добавить файлы в RAM DB с новым batch
for file in response.files:
    add_to_batch(file)
    if batch_size >= config['batch_size']:
        save_files_batch()

# 5. При завершении папки → mark as 'completed'
UPDATE scan_progress SET status='completed' WHERE path=<path>
```

**Гарантия:** Каждый файл обрабатывается ровно один раз благодаря `offset` tracking.

---

## 9. Data Integrity (Целостность данных)

### Проблема #1: PRIMARY KEY Conflicts

**Было:**
```sql
-- tmpfs имеет id: 1,2,3,4,5...
-- disk имеет id: 1000,1001,1002...
-- Попытка INSERT SELECT с id → конфликт!
INSERT INTO files SELECT * FROM tmpfs_files;
-- ❌ Error: UNIQUE constraint failed: files.id
```

**Решение:**
```sql
-- Выбрать БЕЗ id (позволяет target DB генерировать новые ID)
INSERT OR IGNORE INTO files (scan_id, parent_path, name, type, size, md5, created, modified)
SELECT scan_id, parent_path, name, type, size, md5, created, modified
FROM tmpfs_files;
-- ✅ OK! disk DB генерирует новые id: 1003, 1004, 1005...
```

### Проблема #2: Duplicate Files

**Было:** Одна папка обрабатывается дважды (при resume) → дублированные файлы

**Решение:** INSERT OR IGNORE + UNIQUE constraint на (scan_id, parent_path, name)
```sql
CREATE UNIQUE INDEX idx_files_unique ON files(scan_id, parent_path, name);
-- Второе insert тех же файлов → ignored (не добавляются)
```

### Проблема #3: Incomplete Batches

**Было:** При прерывании батч теряется (не записан в RAM DB)

**Решение:** flush_and_terminate() гарантирует save_files_batch() перед exit
```python
def flush_and_terminate():
    storage.save_files_batch()  # Записать ПЕРЕД выходом
    storage.checkpoint_to_disk()
    raise TerminationRequested()
```

### Гарантии целостности

| Сценарий | Гарантия | Механизм |
|----------|----------|----------|
| Graceful SIGTERM | Данные на диске | flush_and_terminate() |
| Crash/Reboot | Последний checkpoint persisted | Disk DB + mark crashed |
| Duplicate files | Игнорируются | INSERT OR IGNORE + UNIQUE |
| Partial batch loss | Не может быть | Batch save перед checkpoint |
| Resume correctness | Один раз per file | offset tracking + folder status |

---

## 10. Классовая структура (Python) - Updated

```python
class StorageManager:
    """Обертка над SQLite. Выполняет CRUD операции с двухуровневым хранилищем."""
    def __init__(self, ram_db_path, disk_db_path):
        self.ram_conn = sqlite3.connect(ram_db_path)  # /dev/shm
        self.disk_conn = sqlite3.connect(disk_db_path)  # monitor.db
    
    def save_files_batch(self):
        """Записать батч из _files_batch в RAM DB"""
        ...
    
    def checkpoint_to_disk(self):
        """Копировать данные из RAM DB на диск с конфигурируемыми порогами"""
        ...
    
    def cleanup_temp_db(self):
        """Удалить tmpfs DB при завершении"""
        ...

class CloudScanner:
    """Сканирование облака с поддержкой resume и graceful interruption"""
    def scan(self, root_path="/"):
        """
        Основной loop сканирования с проверкой _terminate_requested флага
        Вызывает flush_and_terminate() при необходимости
        """
        ...
    
    def is_termination_requested(self):
        """Проверить флаг graceful shutdown"""
        ...
    
    def flush_and_terminate(self):
        """Безопасное завершение: batch → checkpoint → cleanup"""
        ...

class ConfigManager:
    """Управление профилями конфигурации (prod/test)"""
    def load_config(self, profile='prod'):
        """Загрузить ydm_config.json и выбрать профиль"""
        ...

class CLI:
    """Точка входа. Парсинг argparse, установка signal handlers"""
    def run(self):
        """
        1. Загрузить config profile
        2. Установить signal handlers
        3. Запустить сканирование
        4. Catch TerminationRequested exception
        """
        ...
```

---

## 11. Backend Abstraction (`--backend api|rclone`)

*Добавлено 2026-01-10/11, см. [`tasks/rclone_backend/README.md`](../tasks/rclone_backend/README.md)
для полной истории и обоснования.*

Официальный демон `yandex-disk` собирается только под `amd64`/`i386` — на
arm64 (например, Android/Termux) он не работает. Вместо переписывания
ядра под конкретную среду облачный транспорт вынесен за интерфейс:

```python
class CloudResourceClient(ABC):
    """Общий интерфейс источника облачных данных."""
    def get_disk_info(self): ...
    def get_resources(self, path): ...

class YandexClient(CloudResourceClient):
    """Прямые вызовы Yandex Disk REST API (исходная реализация)."""
    ...

class RcloneClient(CloudResourceClient):
    """Обёртка над `rclone` (subprocess: lsjson/about) — не требует
    демона yandex-disk, работает через настроенный remote в rclone.conf."""
    ...
```

Обе реализации отдают одинаковые нормализованные записи в тот же
`StorageManager`/`Analyzer` — весь остальной код (`report diff`,
`duplicates`, `long-paths` и т.д.) не знает и не должен знать, какой
бэкенд использовался. Выбирается глобальным флагом `--backend
{api,rclone}` (default `api`).

## 12. Concurrent-Scan Protection

*Добавлено 2026-01-06 как фикс гонки, при которой второй параллельный
`scan cloud` удалял общую tmpfs-БД первого скана
(`sqlite3.OperationalError: no such table: scan_progress`). Три уровня
защиты:*

1. **Lock file (предотвращение)** — `/tmp/ydm_cloud_scan.lock` с PID
   текущего процесса; перед стартом скана проверяется, жив ли процесс
   из lock-файла (`/proc/<pid>`); мёртвые/битые локи самоочищаются.
2. **Auto-recovery (устойчивость)** — если tmpfs-схема пропала посреди
   скана, `get_connection()` восстанавливает её из последнего
   checkpoint на диске вместо падения.
3. **Process isolation (defense in depth)** — путь tmpfs-БД
   привязан к PID (`/dev/shm/ydm_scan_<pid>.db`), а не общий
   `/dev/shm/ydm_scan.db` — конфликт невозможен, даже если lock
   почему-то не сработал.

Тот же lock-паттерн (PID-файл + `/proc`-проверка + самоочистка)
переиспользован позже для `tools/sync_bisync.py` (см. Этап 7,
[`tasks/rclone_backend/README.md`](../tasks/rclone_backend/README.md)).
