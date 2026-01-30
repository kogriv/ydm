# Финальное Code Review: smart_renamer.py

**Дата:** 06.01.2026  
**Версия кода:** Текущая (285 строк)  
**Статус:** ⚠️ Готов к тестовому запуску, но есть критические проблемы

---

## ✅ Что работает хорошо

1. **Архитектура** - чистое разделение классов, модульная структура
2. **Безопасность** - токен из .env, фильтрация исключенных паттернов
3. **Двухфазный подход** - dry-run → apply
4. **Фильтр по scan_id** - правильно используется в SQL (строка 128)
5. **Collision detection** - `ensure_unique()` работает корректно
6. **Retry логика** - есть обработка 429 ошибок

---

## 🔴 Критические проблемы (нужно исправить)

### 1. LIMIT 500 hardcoded (строка 130)

**Проблема:**
```python
LIMIT 500  # Игнорирует args.limit!
```

**Последствия:**
- SQL всегда выбирает 500 файлов, даже если `--limit 5`
- Лишняя нагрузка на БД
- Медленнее работает

**Исправление:**
```python
def get_long_files(db_path, byte_limit=200, scan_id=44, max_files=500):
    # ...
    query = """
    SELECT parent_path, name, length(cast(name as blob)) as bytes, type
    FROM files
    WHERE scan_id = ? AND bytes > ? AND type IN ('file', 'dir')
    ORDER BY bytes DESC
    LIMIT ?
    """
    cursor.execute(query, (scan_id, byte_limit, max_files))
```

И в main():
```python
long_files = get_long_files(DB_PATH, BYTE_LIMIT, args.scan_id, args.limit)
```

---

### 2. Нет валидации длины AI результата (после строки 249)

**Проблема:**
```python
short_name = ai.generate_short_name(name)
if not short_name:
    print("   -> AI Failed")
    continue
# НЕТ ПРОВЕРКИ ДЛИНЫ!
final_name = ensure_unique(short_name, parent, folder_registries[parent])
```

**Последствия:**
- AI может вернуть имя > 255 байт
- Проблема не решается, файл остается длинным
- Потрачено время на обработку

**Исправление:**
```python
short_name = ai.generate_short_name(name)
if not short_name:
    print("   -> AI Failed")
    continue

# ВАЛИДАЦИЯ ДЛИНЫ
new_name_bytes = len(short_name.encode('utf-8'))
if new_name_bytes > 200:  # Safety margin (ext4 limit is 255)
    print(f"   -> AI returned too long name ({new_name_bytes} bytes), skipping")
    continue

final_name = ensure_unique(short_name, parent, folder_registries[parent])
```

---

### 3. Нет проверки доступности Ollama (строка 225)

**Проблема:**
```python
ai = OllamaClient()
# Сразу начинаем обработку, не проверив доступность
```

**Последствия:**
- Если Ollama не запущен - все запросы упадут
- Потрачено время на ожидание таймаутов
- Непонятные ошибки для пользователя

**Исправление:**
```python
ai = OllamaClient()
print("Testing Ollama connection...")
try:
    test_result = ai.generate_short_name("test_file.pdf")
    if not test_result:
        print("Error: Ollama is not responding. Start it with: ollama serve")
        sys.exit(1)
    print(f"Ollama OK (model: {MODEL_NAME})")
except Exception as e:
    print(f"Error: Cannot connect to Ollama at {OLLAMA_API_URL}")
    print(f"Details: {e}")
    print("Start Ollama with: ollama serve")
    sys.exit(1)
```

---

## 🟡 Важные улучшения (рекомендуется)

### 4. Нет exponential backoff (строка 205)

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

### 5. Нет нормализации путей (строки 195-196)

**Проблема:**
```python
f"{item['parent']}/{item['old_name']}"  # Может быть двойной слэш
```

**Улучшение:**
```python
def build_path(parent, name):
    """Normalize path construction to avoid double slashes."""
    parent = parent.rstrip('/')
    if parent == '' or parent == '/':
        return f"/{name}"
    return f"{parent}/{name}"

# Usage:
ok = client.move(
    build_path(item['parent'], item['old_name']),
    build_path(item['parent'], item['new_name'])
)
```

---

### 6. Нет статистики в dry-run (после строки 278)

**Добавить:**
```python
# Summary statistics
if plan:
    total_bytes_old = sum(item['bytes_old'] for item in plan)
    total_bytes_new = sum(item['bytes_new'] for item in plan)
    savings = total_bytes_old - total_bytes_new
    
    print(f"\n{'='*60}")
    print(f"Plan Summary")
    print(f"{'='*60}")
    print(f"Files to rename: {len(plan)}")
    print(f"Total bytes old: {total_bytes_old:,}")
    print(f"Total bytes new: {total_bytes_new:,}")
    print(f"Bytes saved:      {savings:,} ({100*savings/total_bytes_old:.1f}%)")
    print(f"{'='*60}")
```

---

### 7. Нет success log для rollback (строка 185)

**Добавить:**
```python
success_log = []
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

# После успешного rename (строка 199):
if ok:
    print("  -> Success")
    success_count += 1
    success_log.append({
        "timestamp": datetime.now().isoformat(),
        "old_path": f"{item['parent']}/{item['old_name']}",
        "new_path": f"{item['parent']}/{item['new_name']}"
    })

# В конце apply mode:
if success_log:
    log_file = os.path.join(VAR_DIR, f"rename_success_{timestamp}.json")
    with open(log_file, 'w', encoding='utf-8') as f:
        json.dump(success_log, f, indent=2, ensure_ascii=False)
    print(f"\nSuccess log saved to: {log_file}")
```

---

## 📊 Итоговая оценка

| Критерий | Оценка | Комментарий |
|----------|--------|-------------|
| Функциональность | ⭐⭐⭐⭐ | Базовая логика работает |
| Безопасность | ⭐⭐⭐⭐ | Хорошо, но нужна валидация AI |
| Error handling | ⭐⭐⭐ | Хорошо, но нет проверки Ollama |
| UX | ⭐⭐⭐ | Хорошо, но нет статистики |
| Готовность | ⭐⭐⭐ | **Готов к тестовому запуску с --limit 5-10** |

---

## ✅ Ответ на вопрос: Можно ли запускать?

**ДА, но с оговорками:**

### ✅ Можно запускать для тестирования:
```bash
# Тестовый запуск на 5 файлах
cd /data/pro/ydm/tasks/long_names
python3 smart_renamer.py --limit 5 --scan-id 44
```

**Условия:**
1. ✅ Ollama должен быть запущен (`ollama serve`)
2. ✅ Модель `qwen2.5:7b` должна быть загружена
3. ✅ База данных `monitor.db` должна содержать scan_id 44
4. ⚠️ Будет выбрано 500 файлов из БД (игнорирует --limit), но обработано только 5

### ⚠️ Рекомендуется исправить перед полным запуском:
1. **Критично:** Исправить LIMIT в SQL (использовать args.limit)
2. **Критично:** Добавить валидацию длины AI результата
3. **Важно:** Проверка доступности Ollama
4. **Желательно:** Exponential backoff, статистика, success log

---

## 🚀 Рекомендуемый план действий

### Шаг 1: Быстрое тестирование (сейчас)
```bash
# Проверить что Ollama работает
curl http://localhost:11434/api/generate -d '{"model":"qwen2.5:7b","prompt":"test"}'

# Тестовый запуск
cd /data/pro/ydm/tasks/long_names
python3 smart_renamer.py --limit 5 --scan-id 44
```

### Шаг 2: Исправить критические проблемы
- Исправить LIMIT в SQL
- Добавить валидацию длины AI
- Добавить проверку Ollama

### Шаг 3: Полный запуск
```bash
python3 smart_renamer.py --limit 100 --scan-id 44
```

---

## 📝 Чеклист перед запуском

- [ ] Ollama запущен и доступен на localhost:11434
- [ ] Модель `qwen2.5:7b` загружена (`ollama pull qwen2.5:7b`)
- [ ] База данных `monitor.db` содержит актуальный scan_id
- [ ] Токен Yandex Disk в `.env` файле
- [ ] **Рекомендуется:** Исправить критические проблемы из списка выше

---

**Вывод:** Код функционален и готов к тестовому запуску, но перед полным использованием рекомендуется исправить критические проблемы (особенно LIMIT и валидацию AI результата).

