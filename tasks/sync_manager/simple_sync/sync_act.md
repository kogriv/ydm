## Simple Sync Act: расширение (дизайн)

Документ описывает расширение переходных тулов `sync_tree` и `sync_exclude`:
управление демоном Yandex Disk, локальный скан после перезапуска и
процент синхронизации по данным БД.

---

## Цели
- Перезапуск демона Yandex Disk после изменения `exclude-dirs`.
- Получение статуса демона после перезапуска.
- Автозапуск локального скана через небольшой таймаут.
- Отображение процента синхронизации в дереве (на базе cloud/local снимков).

## Не‑цели
- Полный рефакторинг (см. `tasks/sync_manager/refactoring/`).
- Изменения поведения `ydm.py` вне переходных тулов.

---

## Общее поведение
### По умолчанию (через флаги и алиасы)
- После `sync_exclude --apply` выполняется перезапуск демона.
- Через 3 сек после перезапуска запускается локальный скан.
- `sync_tree` выполняет локальный скан перед построением дерева.
- В дереве отображается `sync_percent` для каждой папки.

### Приоритет вывода (CLI‑AI first)
- **JSON по умолчанию** — основной контракт для автоматизации.
- **Text‑вывод** — вторичный, только для человека.

### Опциональность
Поведение можно отключать отдельными флагами (см. ниже).

---

## Управление демоном Yandex Disk
### Команды
Используются оба способа:
1) `yandex-disk stop` / `yandex-disk start` / `yandex-disk status`
2) `systemctl --user restart yandex-disk`

### Таймауты
- `restart_delay_sec = 3`
- `status_delay_sec = 3` (после старта)

### Логика (псевдокод)
```
stop_start():
  run yandex-disk stop
  run yandex-disk start
  sleep 3
  run yandex-disk status
  run systemctl --user restart yandex-disk
  sleep 3
  run yandex-disk status
```

### Ошибки
- Если команда не найдена — фиксируем в `daemon_log` и продолжаем.
- Если `status` не доступен — фиксируем предупреждение, но не падаем.

### Вывод
Статус демона включается в JSON‑ответ как отдельный блок,
а в text‑выводе показывается краткое сообщение.

---

## Локальный скан после рестарта
### Вариант по умолчанию
- Запуск `ydm.py scan local --path /data/ya_disk`
- Старт через 3 секунды после рестарта демона

### Вывод
- В JSON возвращать `local_scan_started: true/false`
- В text‑выводе краткое сообщение о запуске локального скана

### Ошибки
- Если локальный скан не запустился — ошибка в `local_scan_error`,
  но выполнение `sync_exclude`/`sync_tree` продолжается.

---

## Процент синхронизации (sync_tree)
### Источники
- Cloud snapshot: композитный снимок (как сейчас).
- Local snapshot: новый локальный скан (по умолчанию) или последний успешный локальный скан.

### Метрика
Процент синхронизации папки считается по файлам:
```
sync_percent = (local_files_count / cloud_files_count) * 100
```
Правила:
- Если cloud_files_count == 0 → 100% (пустая папка).
- Если папка не найдена локально → 0%.
- Для partial веток учитывается только пересечение по пути.

### Детали подсчёта
- Считаются **файлы** (`type='file'`), байты — вторично.
- `local_files_count` считается из последнего локального скана.
- `cloud_files_count` считается из композитного cloud‑снимка.

### Вывод
Добавить поле `sync_percent` в JSON‑узлы дерева и в text‑вывод (опционально).

---

## Новые флаги (предложение)
### sync_exclude
- `--restart-daemon / --no-restart-daemon` (default: on)
- `--daemon-status / --no-daemon-status` (default: on)
- `--restart-delay-sec 3`
- `--status-delay-sec 3`
- `--local-scan / --no-local-scan` (default: on)
- `--local-scan-delay-sec 3`
 - `--local-root /data/ya_disk` (default: /data/ya_disk)

### sync_tree
- `--local-scan / --no-local-scan` (default: on)
- `--local-scan-delay-sec 3`
- `--sync-percent / --no-sync-percent` (default: on)
 - `--local-root /data/ya_disk` (default: /data/ya_disk)

---

## JSON‑контракт (дополнения)
### sync_exclude
```
{
  "schema": "sync_exclude:v1",
  "daemon": {
    "restart_attempted": true,
    "status_after_start": "...",
    "status_after_systemctl": "..."
  },
  "local_scan_started": true,
  "local_scan_error": null
}
```

### sync_tree
```
{
  "schema": "sync_tree:v1",
  "root": {
    "path": "/DAO",
    "sync_percent": 66.7
  },
  "local_scan_started": true,
  "local_scan_error": null
}
```

---

## Примечания
- Перезапуск демона обязателен для применения изменений `exclude-dirs`.
- Автоскан локалки дает быстрый фидбек и актуализирует `sync_percent`.
- Все изменения должны быть отключаемыми флагами.
