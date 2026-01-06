# Quickstart для AI-ассистентов и автоматизации

## Важно: Безопасное прерывание сканирования

**Утилита поддерживает graceful interruption!** При SIGTERM/SIGUSR1 сканирование безопасно сохраняется на диск и может быть продолжено позже.

```bash
# Сканирование в фоне
python3 ydm.py scan cloud --progress 2>&1 | tee /tmp/scan.log &
SCAN_PID=$!

# Дождаться прогресса
sleep 10

# Безопасное прерывание (Ctrl+C эквивалент)
kill -TERM $SCAN_PID

# Результат:
# ✓ Данные сохранены на диск
# ✓ Можно продолжить с --resume позже
# ✓ Ничего не потеряется
```

## Выбор профиля конфигурации

Утилита поддерживает два профиля для разных сценариев:

### Production профиль (default)
```bash
# Стабильная работа с редкими checkpoints
python3 ydm.py scan cloud --progress

# Параметры (из ydm_config.json):
# - batch_size: 500 (большие батчи)
# - checkpoint_files_threshold: 10000 (редкие checkpoints)
# - checkpoint_time_sec: 300 (5 минут между checkpoints)
```

### Test профиль (для разработки/быстрого тестирования)
```bash
# Быстрые checkpoints для быстрой обратной связи
python3 ydm.py --config-profile test scan cloud --progress

# Параметры (из ydm_config.json):
# - batch_size: 100 (маленькие батчи)
# - checkpoint_files_threshold: 500 (частые checkpoints)
# - checkpoint_time_sec: 30 (30 сек между checkpoints)

# Это позволяет быстро тестировать interrupt/resume цикл
# без необходимости ждать 5 минут между checkpoints
```

## Минимальный набор команд

### 1. Проверить статус сканов
```bash
python3 ydm.py --format json report scan-list --limit 5 | \
  jq '.data.scans[] | select(.type=="cloud") | {id, status, timestamp}'
```

### 2. Определить действие
**Если есть прерванный скан (status="started" или "interrupted"):**
```bash
# Проверить есть ли pending работа
python3 ydm.py --format json report scan-progress --scan-id <ID> | \
  jq '.data.scan.progress'

# Если pending > 0 - можно продолжить
python3 ydm.py scan cloud --resume --scan-id <ID> --progress 2>&1 | tee /tmp/scan.log &
```

**Если все завершены (status="success") - новый скан:**
```bash
python3 ydm.py scan cloud --progress 2>&1 | tee /tmp/scan_new.log &
```

### 3. Мониторить прогресс
```bash
# В реальном времени
tail -f /tmp/scan.log | jq -r 'select(.status=="progress") | "\(.scanned) files - \(.current)"'

# Из БД (в другом терминале)
python3 ydm.py --format json report scan-progress --scan-id <ID> | \
  jq '.data.scan.progress'
```

### 4. Остановить gracefully
```bash
# Найти PID
ps aux | grep "python3 ydm.py scan cloud" | grep -v grep

# Послать SIGTERM (Ctrl+C)
kill -TERM <PID>  # или просто Ctrl+C

# Данные будут сохранены на диск автоматически
# Проверить статус
python3 ydm.py --format json report scan-info --scan-id <ID> | jq '.data | {status, files_count}'
```

## Handling Long Scans (Работа с долгими сканами)

### Запуск долгого сканирования безопасно

```bash
# 1. Запустить в фоне с логированием
python3 ydm.py scan cloud --progress 2>&1 | tee /tmp/scan_$(date +%s).log &
SCAN_PID=$!

# 2. Проверить что процесс работает
ps -p $SCAN_PID

# 3. В случае необходимости - gracefully остановить
kill -TERM $SCAN_PID

# 4. Проверить что данные сохранились
python3 ydm.py --format json report scan-list --limit 1

# 5. Позже - продолжить с нового терминала
python3 ydm.py scan cloud --resume --progress 2>&1 | tee /tmp/scan_resume.log &
```

### Оценка времени выполнения

```bash
# После 30 сек работы проверить темп
INFO=$(python3 ydm.py --format json report scan-info --scan-id <ID>)
FILES=$(echo "$INFO" | jq -r '.files_count')
DURATION=$(echo "$INFO" | jq -r '.duration')

if [[ $DURATION -gt 0 && $FILES -gt 0 ]]; then
    RATE=$(echo "scale=0; $FILES / $DURATION" | bc)
    echo "Scanning at ~$RATE files/sec"
    
    # Если полный скан может быть 1 млн файлов
    TOTAL_EST=$((1000000 / RATE))
    echo "Est. total time: ~$((TOTAL_EST / 60)) minutes"
fi
```

## Troubleshooting

### Сканирование прервалось - как проверить и продолжить?

```bash
# 1. Проверить статус последнего скана
STATUS=$(python3 ydm.py --format json report scan-list --limit 1 | jq -r '.data.scans[0].status')
SCAN_ID=$(python3 ydm.py --format json report scan-list --limit 1 | jq -r '.data.scans[0].id')

echo "Last scan: ID=$SCAN_ID, status=$STATUS"

# 2. Если interrupted или crashed
if [[ "$STATUS" == "interrupted" ]] || [[ "$STATUS" == "crashed" ]]; then
    # Проверить сколько осталось работы
    PROGRESS=$(python3 ydm.py --format json report scan-progress --scan-id "$SCAN_ID" | \
               jq -r '.data.scan.progress')
    echo "Progress: $PROGRESS"
    
    # Если pending > 0 - можно продолжить
    PENDING=$(echo "$PROGRESS" | jq -r '.pending')
    if [[ $PENDING -gt 0 ]]; then
        echo "Resuming... (pending: $PENDING folders)"
        python3 ydm.py scan cloud --resume --scan-id "$SCAN_ID" --progress
    fi
fi

# 3. Если статус "success" - сканирование было завершено
# Нужно новое сканирование
```

### Данные потеряны при перезагрузке?

```bash
# Нет! monitor.db сохранен и восстанавливается при resume
# tmpfs теряется (это expected), но все важные данные на диске

# Проверить
ls -lh monitor.db        # Должен быть нормальный размер
ls /dev/shm/ydm_scan.db  # Может не существовать (это OK)

# Восстановить последний скан
python3 ydm.py scan cloud --resume --progress
```

### Как использовать test профиль для быстрого feedback?

```bash
# Для разработки и быстрого тестирования
python3 ydm.py --config-profile test scan cloud --path /Books --progress

# Почему test профиль?
# - checkpoint_time_sec: 30 (а не 300) - не ждете 5 минут в тестах
# - checkpoint_files_threshold: 500 - видите checkpoint чаще
# - batch_size: 100 - более тонкая логирование

# Пример interrupt/resume цикла в test профиле:
# 1. Запуск
#    python3 ydm.py --config-profile test scan cloud --path /Books --progress &
# 2. Через 10 сек Ctrl+C (данные уже на диске!)
# 3. Проверка
#    python3 ydm.py --format json report scan-info --scan-id <ID>
# 4. Resume (быстро)
#    python3 ydm.py --config-profile test scan cloud --resume --progress
```

## Важные форматы JSON

### scan-list output
```json
{
  "success": true,
  "data": {
    "total": 5,
    "scans": [
      {
        "id": 5,
        "timestamp": "2026-01-03 06:59:23",
        "type": "cloud",
        "status": "interrupted",
        "duration": 3654.5,
        "files_count": 144089,
        "progress": {
          "total_folders": 217,
          "completed": 150,
          "in_progress": 5,
          "pending": 62
        }
      }
    ]
  }
}
```

### scan-progress output
```json
{
  "success": true,
  "data": {
    "scan": {
      "id": 5,
      "status": "interrupted",
      "progress": {
        "total_folders": 217,
        "completed": 150,
        "pending": 62
      }
    },
    "pending_folders_sample": [
      {"path": "/Books/Archive", "status": "pending", ...}
    ]
  }
}
```

### Progress stream (--progress)
```json
{"status": "progress", "scanned": 1250, "current": "/Photos/2024", "stats": {...}}
{"status": "progress", "scanned": 1300, "current": "/Photos/2025", "stats": {...}}
```

## Примеры jq фильтров

```bash
# Последний cloud scan ID
jq -r '.data.scans[] | select(.type=="cloud") | .id' | head -1

# Статус конкретного скана
jq -r ".data.scans[] | select(.id==$ID) | .status"

# Прогресс в процентах
jq -r '.data.scan.progress | "\((.completed / .total_folders * 100) | floor)%"'

# Pending папки
jq -r '.data.scan.progress.pending'

# Только отсканированные файлы из прогресса
jq -r 'select(.status=="progress") | .scanned'
```

## Полный автоматический workflow

```bash
#!/bin/bash
# Умный resume или новый скан

SCANS=$(python3 ydm.py --format json report scan-list --limit 5)
LAST_ID=$(echo "$SCANS" | jq -r '.data.scans[] | select(.type=="cloud") | .id' | head -1)
LAST_STATUS=$(echo "$SCANS" | jq -r ".data.scans[] | select(.id==$LAST_ID) | .status")

if [[ "$LAST_STATUS" == "started" ]] || [[ "$LAST_STATUS" == "interrupted" ]]; then
    PENDING=$(python3 ydm.py --format json report scan-progress --scan-id "$LAST_ID" | \
              jq -r '.data.scan.progress.pending')
    
    if [[ "$PENDING" -gt 0 ]]; then
        echo "Resuming scan $LAST_ID (pending: $PENDING folders)"
        python3 ydm.py scan cloud --resume --scan-id "$LAST_ID" --progress 2>&1 | \
          tee "/tmp/scan_${LAST_ID}.log" &
    else
        echo "Starting new scan"
        python3 ydm.py scan cloud --progress 2>&1 | tee /tmp/scan_new.log &
    fi
else
    echo "Starting new scan"
    python3 ydm.py scan cloud --progress 2>&1 | tee /tmp/scan_new.log &
fi

echo "Scan started. Monitor with: tail -f /tmp/scan*.log"
```

## Ключевые принципы

✅ **DO:**
- Всегда `--format json` для report
- Всегда `--progress` для scan
- Логировать через `tee`
- Запускать в фоне `&`
- Использовать `jq` для парсинга
- Gracefully останавливать через SIGTERM (kill -TERM)
- Использовать test профиль для разработки/тестирования

❌ **DON'T:**
- Не использовать `timeout` для полных сканов
- Не парсить текстовый вывод
- Не надеяться на tmpfs после reboot
- Не забывать про checkpoint (автоматический)
- Не убивать процесс с SIGKILL (-9) - используйте graceful SIGTERM
