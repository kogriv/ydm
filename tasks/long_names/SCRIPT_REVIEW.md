# Code Review: smart_renamer.py

**Дата:** 06.01.2026
**Ревьюер:** AI Assistant
**Статус:** ✅ Готов к использованию с замечаниями

---

## ✅ Сильные стороны

### 1. Архитектура
- ✅ Чистое разделение на классы: `OllamaClient`, `YandexClientMinimal`
- ✅ Двухфазный подход: `--dry-run` (по умолчанию) → `--apply`
- ✅ Только stdlib (urllib), нет внешних зависимостей
- ✅ Модульная структура соответствует документации

### 2. Безопасность
- ✅ Токен загружается из `.env` (не хардкодится)
- ✅ Фильтрация исключенных паттернов (`_files`, `.git`)
- ✅ Проверка существования БД перед использованием
- ✅ Не трогает локальные файлы, работает только с API

### 3. Надежность
- ✅ Retry логика для 429 ошибок (строки 190-207)
- ✅ Обработка HTTPError с детализацией (404, 409, 429)
- ✅ Collision detection через `ensure_unique()` (строки 144-156)
- ✅ Timestamped план файлы (избегает перезаписи)

### 4. UX
- ✅ Понятные сообщения прогресса
- ✅ Сохранение плана в JSON для ревью
- ✅ Аргументы командной строки с defaults

---

## ⚠️ Критические проблемы

### 🔴 Проблема 1: Query не фильтрует по scan_id (строки 125-131)

**Код:**
```python
query = """
SELECT parent_path, name, length(cast(name as blob)) as bytes
FROM files
WHERE bytes > ? AND type = 'file'
ORDER BY bytes DESC
LIMIT 100
"""
```

**Проблема:** Выбирает файлы из ВСЕХ сканов, не только последнего актуального.

**Последствия:**
- Может выбрать файлы из устаревших сканов
- Может попытаться переименовать уже несуществующие файлы
- Дубликаты из разных сканов

**Решение:**
```python
def get_long_files(db_path, byte_limit=200):
    if not os.path.exists(db_path):
        print(f"Database not found: {db_path}")
        return []

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get latest successful cloud scan
    scan_query = """
    SELECT id FROM scans
    WHERE scan_type = 'cloud' AND status = 'success'
    ORDER BY id DESC LIMIT 1
    """
    scan_row = cursor.execute(scan_query).fetchone()
    if not scan_row:
        print("No successful cloud scans found in database")
        conn.close()
        return []

    scan_id = scan_row[0]
    print(f"Using scan #{scan_id}")

    # Find files where name length in bytes > limit
    query = """
    SELECT parent_path, name, length(cast(name as blob)) as bytes
    FROM files
    WHERE scan_id = ? AND bytes > ? AND type = 'file'
    ORDER BY bytes DESC
    LIMIT 100
    """
    cursor.execute(query, (scan_id, byte_limit))
    results = cursor.fetchall()
    conn.close()
    return results
```

---

### 🟡 Проблема 2: LIMIT 100 игнорирует --limit аргумент (строка 130)

**Код:**
```python
LIMIT 100  # Hardcoded
```

**Проблема:** Аргумент `--limit` используется только в цикле обработки (строка 233), но SQL запрос всегда берет 100 файлов.

**Решение:**
```python
def get_long_files(db_path, byte_limit=200, max_files=100):
    # ...
    query = """
    SELECT parent_path, name, length(cast(name as blob)) as bytes
    FROM files
    WHERE scan_id = ? AND bytes > ? AND type = 'file'
    ORDER BY bytes DESC
    LIMIT ?
    """
    cursor.execute(query, (scan_id, byte_limit, max_files))
```

---

### 🟡 Проблема 3: Нет валидации длины результата AI (строка 248)

**Код:**
```python
short_name = ai.generate_short_name(name)
# ... no validation that len(short_name.encode('utf-8')) < 255
```

**Проблема:** AI может вернуть имя > 255 байт, что не решит проблему.

**Решение:**
```python
short_name = ai.generate_short_name(name)

if not short_name:
    print("   -> AI Failed")
    continue

# ДОБАВИТЬ: Проверка длины
new_name_bytes = len(short_name.encode('utf-8'))
if new_name_bytes > 200:  # Safety margin
    print(f"   -> AI returned too long name ({new_name_bytes} bytes), skipping")
    continue
```

---

## 🟢 Мелкие улучшения

### 1. Проверка доступности Ollama перед стартом (строка 224)

**Текущий код:**
```python
ai = OllamaClient()
# No check if Ollama is running
```

**Улучшение:**
```python
ai = OllamaClient()

# Test Ollama availability
try:
    test_name = ai.generate_short_name("test.pdf")
    if not test_name:
        print("Error: Ollama is not responding. Start it with: ollama serve")
        sys.exit(1)
except Exception as e:
    print(f"Error: Cannot connect to Ollama: {e}")
    sys.exit(1)
```

---

### 2. Exponential backoff вместо fixed delay (строка 204)

**Текущий код:**
```python
time.sleep(10)  # Fixed 10s
```

**Улучшение:**
```python
wait_time = 10 * (2 ** (3 - retries))  # 10s, 20s, 40s
print(f"  -> Rate limit. Waiting {wait_time}s...")
time.sleep(wait_time)
```

---

### 3. Статистика в сухом прогоне (строка 278)

**Добавить после сохранения плана:**
```python
# Summary statistics
total_bytes_old = sum(item['bytes_old'] for item in plan)
total_bytes_new = sum(item['bytes_new'] for item in plan)
savings = total_bytes_old - total_bytes_new

print(f"\n=== Plan Summary ===")
print(f"Files to rename: {len(plan)}")
print(f"Total bytes old: {total_bytes_old:,}")
print(f"Total bytes new: {total_bytes_new:,}")
print(f"Bytes saved: {savings:,} ({100*savings/total_bytes_old:.1f}%)")
print(f"Plan saved to: {plan_file}")
```

---

### 4. Нормализация путей (строка 194-195)

**Текущий код:**
```python
f"{item['parent']}/{item['old_name']}"
```

**Улучшение:**
```python
def build_path(parent, name):
    """Normalize path construction."""
    parent = parent.rstrip('/')
    if parent == '':
        return f"/{name}"
    return f"{parent}/{name}"

# Usage:
ok = client.move(
    build_path(item['parent'], item['old_name']),
    build_path(item['parent'], item['new_name'])
)
```

---

### 5. Логирование успехов для rollback (строка 186-213)

**Добавить журнал:**
```python
# В начале apply mode
success_log = []

# После успешного rename (строка 199)
if ok:
    print("  -> Success")
    success_count += 1
    success_log.append({
        "timestamp": datetime.now().isoformat(),
        "old_path": f"{item['parent']}/{item['old_name']}",
        "new_path": f"{item['parent']}/{item['new_name']}"
    })

# В конце
log_file = os.path.join(VAR_DIR, f"rename_success_{timestamp}.json")
with open(log_file, 'w') as f:
    json.dump(success_log, f, indent=2)
print(f"Success log saved to: {log_file}")
```

---

## 📊 Итоговая оценка

| Критерий | Оценка | Комментарий |
|----------|--------|-------------|
| Архитектура | ⭐⭐⭐⭐⭐ | Отлично, модульная структура |
| Безопасность | ⭐⭐⭐⭐ | Хорошо, но нужен фикс query |
| Error handling | ⭐⭐⭐⭐ | Хорошо, покрыты основные кейсы |
| UX | ⭐⭐⭐⭐ | Хорошо, можно добавить прогресс |
| Документированность | ⭐⭐⭐ | Средне, нужны docstrings |

**Общая оценка:** ⭐⭐⭐⭐ (4/5) - Хорошо, готов к использованию после фикса критической проблемы

---

## ✅ Чеклист перед использованием

- [ ] **Критично:** Исправить query - добавить фильтр по scan_id
- [ ] **Важно:** Добавить валидацию длины результата AI
- [ ] **Желательно:** Проверка доступности Ollama
- [ ] **Опционально:** Exponential backoff
- [ ] **Опционально:** Статистика и логирование

---

## 🎯 Рекомендации по запуску

### Первый запуск (тестирование):
```bash
# 1. Проверить что Ollama запущен
curl http://localhost:11434/api/generate -d '{"model":"qwen2.5:7b","prompt":"test"}'

# 2. Тестовый прогон на 5 файлах
cd /data/pro/ydm/tasks/long_names
python3 smart_renamer.py --limit 5

# 3. Проверить результат
cat ../../var/rename_plan_*.json | jq '.[] | {old: .old_name, new: .new_name}'

# 4. Если все ОК - применить
python3 smart_renamer.py --apply --plan-file ../../var/rename_plan_XXXXXX.json
```

### После фикса критической проблемы:
```bash
# Полный прогон
python3 smart_renamer.py --limit 100
```
