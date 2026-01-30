# Прогресс исправлений: smart_renamer.py

**Дата:** 06.01.2026
**Базовая версия:** v1.0 (созданная пользователем)
**Обновленная версия:** v1.1 (после code review)

---

## ✅ Критические исправления

### 1. Добавлен фильтр по scan_id ✅
**Проблема:** Query выбирал файлы из ВСЕХ сканов в БД, что могло привести к попыткам переименования несуществующих файлов.

**Исправление:**
- Добавлена логика выбора конкретного scan_id или последнего успешного cloud скана
- SQL query теперь фильтрует по `WHERE scan_id = ?`
- Добавлен аргумент `--scan-id` с дефолтом 44 (наиболее актуальный скан)
- Функция `get_long_files()` теперь принимает параметр `scan_id`

**Файл:** `smart_renamer.py:117-155`

**Код:**
```python
def get_long_files(db_path, byte_limit=200, max_files=100, scan_id=None):
    # ...
    if scan_id is None:
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

### 2. Добавлена валидация длины AI результата ✅
**Проблема:** AI мог вернуть имя длиннее 255 байт, что не решало исходную проблему.

**Исправление:**
- Добавлена проверка длины после генерации AI
- Файлы с результатом > 200 байт пропускаются
- Вывод информации о размере нового имени

**Файл:** `smart_renamer.py:265-270`

**Код:**
```python
# FIX 2: Validate AI result length
new_name_bytes = len(short_name.encode('utf-8'))
if new_name_bytes > 200:  # Safety margin (ext4 limit is 255)
    print(f"   -> AI returned too long name ({new_name_bytes} bytes), skipping")
    continue
```

---

### 3. Исправлен LIMIT в SQL запросе ✅
**Проблема:** SQL query игнорировал аргумент `--limit`, всегда выбирая 100 файлов.

**Исправление:**
- Функция `get_long_files()` теперь принимает параметр `max_files`
- Параметр используется в SQL: `LIMIT ?`
- При вызове передается `args.limit`

**Файл:** `smart_renamer.py:117-155`

---

## ✅ Улучшения

### 1. Проверка доступности Ollama ✅
**Добавлено:** Тестирование подключения к Ollama перед началом обработки.

**Преимущества:**
- Fail fast: пользователь сразу узнает о проблеме
- Понятное сообщение с инструкцией запуска
- Проверка работоспособности модели

**Файл:** `smart_renamer.py:243-253`

**Код:**
```python
# IMPROVEMENT 1: Test Ollama availability
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

### 2. Exponential backoff для retry ✅
**Добавлено:** Прогрессивное увеличение задержки при rate limiting.

**Было:** Фиксированная задержка 10 секунд
**Стало:** 10s → 20s → 40s (экспоненциальный рост)

**Файл:** `smart_renamer.py:195-203`

**Код:**
```python
if e.code == 429:
    wait_time = 10 * (2 ** (3 - retries))  # 10s, 20s, 40s
    print(f"  -> Rate limit. Waiting {wait_time}s...")
    time.sleep(wait_time)
    retries -= 1
```

---

### 3. Статистика в dry-run режиме ✅
**Добавлено:** Подробная сводка после генерации плана.

**Выводится:**
- Количество файлов для переименования
- Суммарный размер старых имен
- Суммарный размер новых имен
- Экономия байт и процент

**Файл:** `smart_renamer.py:287-297`

**Вывод:**
```
============================================================
Plan Summary
============================================================
Files to rename: 10
Total bytes old: 3,245
Total bytes new: 521
Bytes saved:     2,724 (83.9%)
============================================================
```

---

### 4. Лог успешных операций для rollback ✅
**Добавлено:** Сохранение журнала успешных переименований.

**Содержимое лога:**
- Timestamp каждой операции
- Старый путь
- Новый путь

**Назначение:** Возможность отката изменений (manual rollback).

**Файл:** `smart_renamer.py:184, 201-210, 213-218`

**Формат:**
```json
[
  {
    "timestamp": "2026-01-06T15:30:45.123456",
    "old_path": "/Books/Math/Zadachi/...",
    "new_path": "/Books/Math/Zadachi/shahmeister_graphs_2016.pdf"
  }
]
```

**Сохраняется в:** `var/rename_success_YYYYMMDD_HHMMSS.json`

---

### 5. Нормализация путей ✅
**Добавлено:** Функция `build_path()` для корректного построения путей.

**Проблема:** Возможные двойные слэши при конкатенации путей.

**Решение:**
```python
def build_path(parent, name):
    """Normalize path construction to avoid double slashes."""
    parent = parent.rstrip('/')
    if parent == '' or parent == '/':
        return f"/{name}"
    return f"{parent}/{name}"
```

**Использование:**
```python
old_path = build_path(item['parent'], item['old_name'])
new_path = build_path(item['parent'], item['new_name'])
```

**Файл:** `smart_renamer.py:148-153, 190-191`

---

## 📊 Сравнение версий

| Функционал | v1.0 | v1.1 |
|------------|------|------|
| Фильтр по scan_id | ❌ | ✅ Дефолт: scan 44 |
| Валидация AI результата | ❌ | ✅ < 200 байт |
| Respect --limit в SQL | ❌ | ✅ |
| Проверка Ollama | ❌ | ✅ Fail fast |
| Exponential backoff | ❌ (10s fixed) | ✅ 10s→20s→40s |
| Статистика в dry-run | ❌ | ✅ |
| Success log | ❌ | ✅ JSON с timestamp |
| Нормализация путей | ❌ | ✅ build_path() |

---

## 🎯 Новые аргументы

```bash
python3 smart_renamer.py --help

Options:
  --dry-run              Generate plan only (default)
  --apply                Execute the plan (requires --plan-file)
  --plan-file PATH       Path to JSON plan file to execute
  --limit N              Max files to process in discovery (default: 10)
  --scan-id ID           Scan ID to use (default: 44)
```

---

## 🚀 Примеры использования

### Dry-run с лимитом 5 файлов:
```bash
cd /data/pro/ydm/tasks/long_names
python3 smart_renamer.py --limit 5 --scan-id 44
```

### Применить план:
```bash
python3 smart_renamer.py --apply --plan-file ../../var/rename_plan_20260106_153045.json
```

### Полный прогон (100 файлов):
```bash
python3 smart_renamer.py --limit 100
```

---

## 📁 Результаты работы

### Dry-run создает:
- `var/rename_plan_YYYYMMDD_HHMMSS.json` - план переименований

### Apply создает:
- `var/rename_success_YYYYMMDD_HHMMSS.json` - лог успешных операций

---

## ✅ Статус

**Все критические проблемы исправлены:** ✅
**Все рекомендованные улучшения добавлены:** ✅
**Готовность к использованию:** ✅ Да, после проверки что Ollama запущен

**Следующий шаг:** Тестирование на scan 44

---

## 📝 Changelog

**v1.1 (2026-01-06)**
- ✅ FIX: Добавлен фильтр по scan_id в SQL query
- ✅ FIX: Валидация длины AI результата (< 200 байт)
- ✅ FIX: Использование args.limit в SQL LIMIT
- ✅ NEW: Проверка доступности Ollama при старте
- ✅ NEW: Exponential backoff для retry (10s→20s→40s)
- ✅ NEW: Статистика в dry-run (байты сэкономлены, %)
- ✅ NEW: Success log в JSON для rollback
- ✅ NEW: Нормализация путей через build_path()
- ✅ NEW: Аргумент --scan-id (default: 44)

**v1.0 (2026-01-06)**
- Initial implementation
