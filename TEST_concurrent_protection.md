# Тестирование защиты от конкурентных сканов

## Подготовка
```bash
cd /data/infra/ya_disk/tools

# Убедиться что нет активных сканов
ps aux | grep "ydm.py.*scan" | grep -v grep

# Убедиться что нет lock файлов
ls -la /tmp/ydm_cloud_scan.lock 2>/dev/null || echo "No lock file (OK)"

# Убедиться что tmpfs чистый
ls -la /dev/shm/ydm_scan_*.db 2>/dev/null || echo "No tmpfs DBs (OK)"
```

## Test 1: Lock Prevention (Уровень 1)

**Цель:** Проверить что второй скан блокируется lock файлом

```bash
# Терминал 1: Запуск первого скана
python3 ydm.py --config-profile test scan cloud --progress 2>&1 | tee /tmp/scan_test1.log &
SCAN1_PID=$!

echo "Scan 1 started with PID: $SCAN1_PID"
echo "Waiting 3 minutes for scan to initialize..."
sleep 180  # Ждем 3 минуты для инициализации

# Проверить что скан работает
ps -p $SCAN1_PID && echo "✓ Scan 1 is running"

# Проверить lock файл
cat /tmp/ydm_cloud_scan.lock
echo "Lock PID: $(cat /tmp/ydm_cloud_scan.lock)"

# Проверить tmpfs DB (должен быть process-specific)
ls -lh /dev/shm/ydm_scan_${SCAN1_PID}.db

# Терминал 2: Попытка запустить второй скан (должна FAIL)
echo "Attempting second scan (should be blocked)..."
python3 ydm.py --config-profile test scan cloud --progress

# Ожидаемый результат:
# {
#   "error": "concurrent_scan",
#   "message": "Another scan is already running (PID: ...)",
#   "lock_pid": <SCAN1_PID>
# }

# Cleanup
kill -TERM $SCAN1_PID
sleep 5
ls /tmp/ydm_cloud_scan.lock 2>/dev/null && echo "ERROR: Lock not removed!" || echo "✓ Lock removed"
```

## Test 2: Stale Lock Cleanup

**Цель:** Проверить автоматическую очистку устаревших lock файлов

```bash
# Создать поддельный lock с несуществующим PID
echo "99999" > /tmp/ydm_cloud_scan.lock

# Попытка запуска (должна успешно удалить stale lock)
python3 ydm.py --config-profile test scan cloud --path /Books --progress &
SCAN_PID=$!

sleep 5
ps -p $SCAN_PID && echo "✓ Scan started despite stale lock" || echo "ERROR: Scan failed to start"

# Cleanup
kill -TERM $SCAN_PID
```

## Test 3: Process-Specific tmpfs DB (Уровень 3)

**Цель:** Проверить что каждый процесс использует свой tmpfs файл

```bash
# Запустить скан
python3 ydm.py --config-profile test scan cloud --path /Books --progress &
SCAN_PID=$!

sleep 180  # Ждем инициализации

# Проверить что tmpfs DB содержит PID в имени
TMPFS_DB="/dev/shm/ydm_scan_${SCAN_PID}.db"
if [ -f "$TMPFS_DB" ]; then
    echo "✓ Process-specific tmpfs DB exists: $TMPFS_DB"
    ls -lh "$TMPFS_DB"

    # Проверить что старый shared DB НЕ существует
    [ ! -f "/dev/shm/ydm_scan.db" ] && echo "✓ Old shared DB does not exist" || echo "ERROR: Old shared DB exists!"
else
    echo "ERROR: Process-specific tmpfs DB not found!"
fi

# Cleanup
kill -TERM $SCAN_PID
sleep 5

# Проверить что tmpfs DB удален
[ ! -f "$TMPFS_DB" ] && echo "✓ tmpfs DB cleaned up" || echo "ERROR: tmpfs DB not cleaned up"
```

## Test 4: Auto-Recovery (Уровень 2)

**Цель:** Проверить восстановление при потере схемы

**⚠️ Опасный тест - имитирует удаление tmpfs DB во время работы**

```bash
# Запустить скан с test профилем
python3 ydm.py --config-profile test scan cloud --path /Books --progress 2>&1 | tee /tmp/scan_recovery.log &
SCAN_PID=$!

echo "Scan started with PID: $SCAN_PID"
echo "Waiting 4 minutes for initial checkpoint..."
sleep 240

# Проверить что есть checkpoint на диске
LAST_SCAN=$(python3 ydm.py --format json report scan-list --limit 1 | jq -r '.data.scans[0].id')
echo "Last scan ID: $LAST_SCAN"

python3 ydm.py --format json report scan-info --scan-id $LAST_SCAN | jq '.data | {status, files_count}'

# Симулировать потерю tmpfs DB (удалить файл вручную)
TMPFS_DB="/dev/shm/ydm_scan_${SCAN_PID}.db"
echo "Simulating tmpfs DB loss..."
rm -f "$TMPFS_DB"

echo "Waiting 60 seconds for auto-recovery to trigger..."
sleep 60

# Проверить логи на наличие сообщения о recovery
if grep -q "CRITICAL: tmpfs DB schema lost" /tmp/scan_recovery.log; then
    echo "✓ Recovery mechanism triggered"
    grep "Recovery complete" /tmp/scan_recovery.log && echo "✓ Recovery successful"
else
    echo "⚠️  Recovery not triggered or scan crashed"
fi

# Проверить что скан продолжает работать
if ps -p $SCAN_PID > /dev/null; then
    echo "✓ Scan still running after recovery"
else
    echo "ERROR: Scan crashed after DB loss"
fi

# Cleanup
kill -TERM $SCAN_PID
```

## Test 5: Fast Checkpoint Cycle (Test Profile)

**Цель:** Проверить что test профиль действительно делает частые checkpoints

```bash
# Запуск с test профилем
python3 ydm.py --config-profile test scan cloud --path /Books --progress 2>&1 | tee /tmp/scan_checkpoint.log &
SCAN_PID=$!

# Мониторить checkpoints в реальном времени
echo "Monitoring checkpoints (should appear every 30 sec or 500 files)..."
tail -f /tmp/scan_checkpoint.log | grep -i checkpoint &
TAIL_PID=$!

# Через 2 минуты - должно быть несколько checkpoints
sleep 120

# Проверить количество checkpoints в логе
CHECKPOINT_COUNT=$(grep -c "checkpoint" /tmp/scan_checkpoint.log)
echo "Checkpoints detected: $CHECKPOINT_COUNT"

if [ $CHECKPOINT_COUNT -ge 3 ]; then
    echo "✓ Test profile working (frequent checkpoints)"
else
    echo "⚠️  Few checkpoints detected (may need more time or data)"
fi

# Cleanup
kill -TERM $TAIL_PID
kill -TERM $SCAN_PID
```

## Мониторинг во время тестов

```bash
# В отдельном терминале - непрерывный мониторинг
watch -n 5 'echo "=== Active scans ===" && \
    ps aux | grep "ydm.py.*scan" | grep -v grep && \
    echo -e "\n=== Lock file ===" && \
    (ls -la /tmp/ydm_cloud_scan.lock 2>/dev/null || echo "No lock") && \
    echo -e "\n=== tmpfs DBs ===" && \
    (ls -lh /dev/shm/ydm_scan_*.db 2>/dev/null || echo "No tmpfs DBs")'
```

## Ожидаемые результаты

| Test | Expected Result |
|------|----------------|
| Test 1 | ✓ Second scan blocked with lock error |
| Test 2 | ✓ Stale lock auto-removed, scan starts |
| Test 3 | ✓ tmpfs DB path contains PID |
| Test 4 | ✓ Auto-recovery from schema loss |
| Test 5 | ✓ Checkpoints every ~30 sec with test profile |

## Troubleshooting

**Если скан не стартует:**
```bash
# Проверить логи
tail -100 /tmp/scan_*.log

# Проверить ошибки API
python3 ydm.py --format json report scan-list --limit 1 | jq '.data.scans[0] | {status, duration, files_count}'
```

**Если lock не отпускается:**
```bash
# Принудительно удалить
rm -f /tmp/ydm_cloud_scan.lock

# Проверить зомби-процессы
ps aux | grep ydm.py | grep -v grep
```

**Если tmpfs DB не удаляется:**
```bash
# Принудительная очистка
rm -f /dev/shm/ydm_scan_*.db
```
