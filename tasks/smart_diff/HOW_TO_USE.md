# Как использовать Smart Diff с эвристикой

## Быстрый старт

### 1. Посмотреть текущий эталонный полный скан

```bash
python3 ydm.py report full-scan-info
```

Показывает, какой скан используется как базовый для сравнения (эвристика автоматически выбрала scan 47).

### 2. Посмотреть кандидатов в полный скан

```bash
python3 ydm.py report full-scan-candidates
```

Показывает все сканы в окне свежести с метриками:
- `id`, `timestamp`, `status`, `files_count`
- `progress` (completed/in_progress/pending)
- `top_levels` (верхнеуровневые папки)
- `f_files` (доля от максимума)
- `is_best` (помечает лучший кандидат)

### 3. Запустить сравнение облака и локала

**Текстовый формат:**
```bash
python3 ydm.py report diff
```

**JSON формат (для скриптов):**
```bash
python3 ydm.py --format json report diff
```

## Что вы увидите

### В текстовом формате:

```
Comparing cloud scan composite(base=47, partials=4) with local scan 2

Missing on local (3565 files):
  /DAO/Popov_V._Dao_Kniga_Nachalo.a4-1.pdf
  /DAO/Popov_V._Dao_Kniga_Nachalo.a6-1.pdf
  ...

Missing in cloud (25735 files):
  .sync/cli.log
  .sync/core-1.log.gz
  ...
```

### В JSON формате:

```json
{
  "success": true,
  "data": {
    "compare_scans": {
      "cloud": "composite(base=47, partials=4)",
      "local": 2
    },
    "missing_local_count": 3565,
    "missing_cloud_count": 25735,
    "missing_local_sample": [...],
    "missing_cloud_sample": [...],
    "composite_info": {
      "base_scan_id": 47,
      "updated_folders_count": 4
    }
  }
}
```

## Ключевые отличия от старой версии

### Старая версия:
- Использовала последний cloud scan (мог быть частичным)
- Если последний скан был только для одной папки → неактуальное сравнение

### Новая версия (с эвристикой):
- ✅ Использует **последний полный скан** (scan 47) как базу
- ✅ Добавляет обновления из частичных сканов (scan 48, 49)
- ✅ Показывает актуальное состояние облака

## Настройка

### Изменить окно свежести

В `ydm_config.json`:
```json
{
  "prod": {
    "full_scan_fresh_window_days": 3,
    ...
  }
}
```

### Явно указать эталонный скан

В `ydm_config.json`:
```json
{
  "prod": {
    "reference_full_scan_id": 47,
    ...
  }
}
```

## Дополнительные опции

### Сравнение с конкретными сканами

```bash
# Указать конкретный cloud scan
python3 ydm.py report diff --cloud-scan-id 47

# Указать конкретный local scan
python3 ydm.py report diff --local-scan-id 2

# Отключить композитный режим (старое поведение)
python3 ydm.py report diff --no-composite
```

### Просмотр метаданных скана

```bash
# Информация о конкретном скане
python3 ydm.py report scan-info --scan-id 47
```

## Примеры использования

### 1. Быстрая проверка различий

```bash
python3 ydm.py report diff | head -20
```

### 2. Получить только количество различий

```bash
python3 ydm.py --format json report diff | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data.get('success'):
    d = data['data']
    print(f\"Missing on local: {d['missing_local_count']} files\")
    print(f\"Missing in cloud: {d['missing_cloud_count']} files\")
"
```

### 3. Проверить, какой скан используется

```bash
python3 ydm.py report full-scan-info
python3 ydm.py report full-scan-candidates | grep -A 5 '"is_best": true'
```

## Устранение проблем

### Проблема: "No full scan found"

**Решение:** Проверьте, есть ли успешные cloud-сканы в окне свежести:
```bash
python3 ydm.py report full-scan-candidates
```

Если список пуст, увеличьте `full_scan_fresh_window_days` в конфиге.

### Проблема: Используется старый скан

**Решение:** Явно укажите нужный скан в конфиге:
```json
{
  "prod": {
    "reference_full_scan_id": 47
  }
}
```

### Проблема: Неактуальные результаты

**Решение:** Убедитесь, что:
1. Есть свежий полный скан облака
2. Окно свежести настроено правильно
3. Композитный режим включен (по умолчанию)

## Дополнительная информация

- Полное описание: `tasks/smart_diff/TASK_SMART_DIFF.md`
- Эвристика: `tasks/smart_diff/FULL_SCAN_HEURISTICS.md`
- Результаты тестов: `tasks/smart_diff/HEURISTICS_TEST_RESULTS.md`

