# ISSUE: Множественные дубликаты файлов при Checkpoint

**Дата обнаружения:** 6 января 2026
**Статус:** ✅ Исправлено
**Компонент:** `StorageManager.checkpoint_to_disk`

## Описание проблемы
При использовании механизма `checkpoint_to_disk` (сброс данных из RAM в БД на диске) происходит многократное дублирование записей в таблице `files` основной базы данных.

### Симптомы
1.  Экспоненциальный рост размера базы `monitor.db`.
2.  Для скана #41 зафиксировано по **160 копий** одного и того же файла в корневых папках (`/Books`, `/Ingos` и т.д.).
3.  SQL-запрос подтверждения:
    ```sql
    SELECT parent_path, name, COUNT(*) as count 
    FROM files 
    WHERE scan_id = 41 
    GROUP BY parent_path, name 
    HAVING count > 1;
    ```

## Техническая причина
В методе `checkpoint_to_disk` используется запрос:
```python
temp_conn.execute("""
    INSERT INTO disk.files 
    (scan_id, parent_path, name, type, size, md5, created, modified)
    SELECT scan_id, parent_path, name, type, size, md5, created, modified FROM main.files
""")
```

**Логическая ошибка:**
1.  Таблица `main.files` (в RAM) накапливает файлы с начала сессии сканирования (accumulative buffer).
2.  Каждый вызов `checkpoint_to_disk` (по таймеру или лимиту файлов) копирует **ВСЕ** содержимое `main.files` в `disk.files`.
3.  Поскольку в `main.files` данные не очищаются после чекпоинта (чтобы поддержать целостность данных в RAM), они повторно записываются на диск.

Пример:
- Чекпоинт 1: RAM [A, B] -> Disk [A, B]
- Чекпоинт 2: RAM [A, B, C] -> Disk [A, B, A, B, C]
- Итог на диске: A(2), B(2), C(1)

## Предлагаемое решение

### 1. Добавить UNIQUE Constraint (Schema Fix)
Необходимо гарантировать уникальность файла в рамках одного сканирования на уровне БД.

**SQL миграция:**
```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_files_unique 
ON files(scan_id, parent_path, name);
```

### 2. Изменить логику Checkpoint (Code Fix)
Вместо безусловного `INSERT` использовать `INSERT OR IGNORE` (или `REPLACE`).

```python
temp_conn.execute("""
    INSERT OR IGNORE INTO disk.files 
    (...)
    SELECT ... FROM main.files
""")
```

### 3. Очистка существующих данных
Необходимо удалить дубликаты из `monitor.db` для пострадавших сканов (в частности #41).

**SQL скрипт очистки:**
```sql
DELETE FROM files 
WHERE id NOT IN (
    SELECT MIN(id) 
    FROM files 
    GROUP BY scan_id, parent_path, name
);
```

## Влияние на производительность
- Отсутствие уникального индекса приводило к быстрым вставкам, но раздуванию БД.
- Добавление индекса замедлит вставку (`INSERT`), но `OR IGNORE` предотвратит дублирование.
- Чтение (SELECT) ускорится за счет наличия индекса.

---

## ✅ Реализация исправлений

**Дата исправления:** 6 января 2026

### Выполнено:

1. ✅ **UNIQUE индекс добавлен в схему БД**
   - Добавлен в `_init_final_db()` (строка 316)
   - Добавлен в `init_db()` (строка 431)
   - Индекс: `idx_files_unique ON files(scan_id, parent_path, name)`

2. ✅ **Исправлена логика checkpoint**
   - Изменено `INSERT INTO` → `INSERT OR IGNORE INTO` в `checkpoint_to_disk()` (строка 238)
   - Добавлен комментарий с объяснением

3. ✅ **Добавлена команда очистки дубликатов**
   - Новый метод `Analyzer.clean_duplicates(scan_id=None)`
   - Команда CLI: `python3 ydm.py report clean-duplicates [--scan-id ID]`
   - Поддерживает очистку конкретного скана или всех сканов
   - Возвращает статистику: количество удаленных дубликатов, файлов до/после

### Использование команды очистки:

```bash
# Очистить дубликаты для конкретного скана (например, scan_id=41)
python3 ydm.py report clean-duplicates --scan-id 41

# Очистить дубликаты для всех сканов
python3 ydm.py report clean-duplicates

# Вывод в JSON формате
python3 ydm.py --format json report clean-duplicates --scan-id 41
```

### Для существующих БД:

Для применения UNIQUE индекса к существующим базам данных выполните:
```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_files_unique 
ON files(scan_id, parent_path, name);
```

Затем выполните очистку дубликатов через команду CLI (см. выше).
