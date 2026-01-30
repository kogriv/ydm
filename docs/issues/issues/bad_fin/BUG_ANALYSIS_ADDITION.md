# Дополнение к анализу bad_fin_issue.md

## Точная локализация бага

### Найденный баг в коде (ydm.py:861-985)

**Проблема в строках 982-983:**

```python
# Line 897: while queue:  <-- Начало главного цикла
#     ... обработка папки ...
#     Line 904: storage.update_folder_status(scan_id, current_path, 'in_progress')
#     Line 921-977: Внутренний цикл (пагинация API)
#     ... конец итерации главного цикла ...
# Line 978: <-- Конец while queue

# Line 979-980: Сохранение остатков batch
if batch:
    storage.save_files_batch(batch)

# Line 982-983: КРИТИЧЕСКИЙ БАГ!
# Mark scan as completed
storage.update_folder_status(scan_id, current_path, 'completed')
```

**Что не так:**
1. `update_folder_status(..., 'completed')` находится **ВНЕ** цикла `while queue:`
2. Она вызывается только **ОДИН РАЗ** после выхода из цикла
3. Переменная `current_path` содержит значение **последней** обработанной папки
4. Все остальные папки остаются в статусе `'in_progress'`

### Правильное поведение

Строка 983 должна быть **ВНУТРИ** цикла `while queue:`, после завершения внутреннего цикла пагинации (после line 977):

```python
while queue:
    current_path = queue.pop(0)
    # ... обработка ...

    # Mark as in_progress
    storage.update_folder_status(scan_id, current_path, 'in_progress')

    # Внутренний цикл пагинации
    while True:
        # ... обработка items ...
        if offset >= total:
            break

    # ✅ ПРАВИЛЬНО: Отметить папку как completed ЗДЕСЬ
    storage.update_folder_status(scan_id, current_path, 'completed')

# После выхода из while queue - все папки уже completed
if batch:
    storage.save_files_batch(batch)
```

## Почему scan status остался 'started'?

Проверил код вызова `finish_scan()`:

**ydm.py:1628-1631:**
```python
files_count = scanner.scan(scan_id, storage, resume=self.args.resume, start_path=start_path)

duration = time.time() - start_time
storage.finish_scan(scan_id, 'success', duration)
```

`finish_scan()` вызывается **ПОСЛЕ** возврата из `scanner.scan()`.

**НО!** Есть тонкость:
- `scanner.scan()` вернул управление
- Следующая строка должна была вызвать `finish_scan(scan_id, 'success', ...)`
- Но в логе скана 44 мы видим итоговый JSON с прогрессом, а не с success

### Возможные причины

1. **Checkpoint не сохранился на диск:**
   - `finish_scan()` вызывается на tmpfs DB
   - `checkpoint_to_disk()` не был вызван после `finish_scan()`
   - tmpfs DB была удалена → изменения потеряны

2. **Проверим код финализации (ydm.py:1600-1610):**
```python
finally:
    if scan_success:
        storage.finalize()

    # Level 1: Release lock file
    if os.path.exists(LOCK_FILE):
        try:
            os.remove(LOCK_FILE)
```

`storage.finalize()` вызывается в `finally` блоке. Проверим что он делает:

**ydm.py:154-167:**
```python
def finalize(self):
    """Сохраняет данные из временной БД на диск после успешного завершения."""
    if self.temp_mode and os.path.exists(self.db_path):
        try:
            # Последний checkpoint перед финализацией
            self.checkpoint_to_disk(force=True)

            # Удалить временную БД - данные уже на диске через checkpoint
            self.cleanup_temp_db()
            return True
        except Exception as e:
            print(f"Warning: Failed to finalize scan to disk: {e}", file=sys.stderr)
            return False
    return True
```

**Ага! Вот проблема:**

1. `finish_scan(scan_id, 'success', duration)` обновляет статус в **tmpfs DB**
2. `finalize()` делает `checkpoint_to_disk(force=True)`
3. НО `checkpoint_to_disk()` использует `INSERT OR IGNORE` для таблицы `scans`!

**Проверим checkpoint_to_disk() (ydm.py:193-197):**
```python
# Copy scans (if not exists)
temp_conn.execute("""
    INSERT OR IGNORE INTO disk.scans
    SELECT * FROM main.scans
""")
```

**INSERT OR IGNORE = если запись с таким ID уже есть, игнорировать!**

### Полная картина бага

1. Scan 44 стартовал → запись в `scans` создана со статусом `'started'`
2. Промежуточные checkpoints сохранили эту запись на диск: `INSERT OR IGNORE`
3. Scan завершился → `finish_scan()` обновил статус на `'success'` в tmpfs
4. `finalize()` вызвал `checkpoint_to_disk(force=True)`
5. `checkpoint_to_disk()` выполнил `INSERT OR IGNORE` → **ИГНОРИРОВАЛ обновление**, т.к. запись с scan_id=44 уже есть на диске!
6. tmpfs DB удалена → обновление статуса потеряно

## Резюме всех багов

### Баг 1: Папки не помечаются как completed
- **Локация:** ydm.py:983
- **Проблема:** `update_folder_status(..., 'completed')` вне цикла
- **Эффект:** Все папки кроме последней остаются `in_progress`

### Баг 2: Scan status не обновляется на success
- **Локация:** ydm.py:193-197 (checkpoint_to_disk)
- **Проблема:** `INSERT OR IGNORE` не обновляет существующие записи
- **Эффект:** Статус скана остается `'started'` вместо `'success'`

### Баг 3: scan_progress статусы не синхронизируются
- **Локация:** ydm.py:212-216 (checkpoint_to_disk)
- **Проблема:** `INSERT OR REPLACE` для scan_progress, но папки не помечены completed
- **Эффект:** На диске сохраняются папки со статусом `in_progress`

## Необходимые исправления

### Фикс 1: Переместить update_folder_status внутрь цикла

```python
while queue:
    current_path = queue.pop(0)
    # ...
    storage.update_folder_status(scan_id, current_path, 'in_progress')

    # Пагинация
    while True:
        # ...
        if offset >= total:
            break

    # ✅ ДОБАВИТЬ ЗДЕСЬ
    storage.update_folder_status(scan_id, current_path, 'completed')
```

### Фикс 2: Использовать UPDATE для scans в checkpoint

```python
# Вместо INSERT OR IGNORE
temp_conn.execute("""
    INSERT INTO disk.scans SELECT * FROM main.scans
    ON CONFLICT(id) DO UPDATE SET
        status = excluded.status,
        duration = excluded.duration
""")
```

### Фикс 3: Принудительный checkpoint после finish_scan

```python
# ydm.py:1631 после finish_scan
storage.finish_scan(scan_id, 'success', duration)
storage.checkpoint_to_disk(force=True)  # ✅ ДОБАВИТЬ
```

## Приоритет

1. **Критичный:** Фикс 2 (статус скана)
2. **Критичный:** Фикс 1 (статусы папок)
3. **Желательный:** Фикс 3 (страховка)
