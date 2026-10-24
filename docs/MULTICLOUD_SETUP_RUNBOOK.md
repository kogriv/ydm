# Runbook: первичная настройка YDM для нескольких облаков

Этот runbook доводит YDM до готового cloud inventory, но **не включает
синхронизацию**. После него локальное зеркало Google Drive остаётся пустым,
policy не содержит папок, bisync baseline отсутствует, демон Яндекс.Диска не
запускается и не возобновляется.

## Размещение на этом хосте

| Назначение | Путь |
|---|---|
| Локальное зеркало Яндекс.Диска | `/data/ya_disk` |
| Локальное зеркало Google Drive | `/data/gdrive` |
| Состояние Yandex target | `/data/ydm/yandex` |
| Состояние Google target | `/data/ydm/gdrive` |
| Конфигурация targets | `$HOME/.config/ydm/targets.json` |

`state_dir` содержит `monitor.db`, policy, filters, locks и журналы. Он не
должен находиться внутри локального зеркала. OAuth-токены в targets-файл не
записываются: Google credentials остаются в `rclone.conf`, Yandex token — в
окружении или `.env`.

## 1. Подготовить каталоги

```bash
sudo mkdir -p /data/gdrive /data/ydm/gdrive /data/ydm/yandex
sudo chown -R "$USER:$USER" /data/gdrive /data/ydm
chmod 700 /data/ydm /data/ydm/gdrive /data/ydm/yandex
mkdir -p "$HOME/.config/ydm"
chmod 700 "$HOME/.config/ydm"
```

## 2. Создать targets-файл

Создать `$HOME/.config/ydm/targets.json`:

```json
{
  "schema": "ydm_targets:v1",
  "default_target": "gdrive",
  "targets": {
    "yandex": {
      "provider": "yandex",
      "inventory": {"kind": "yandex_api"},
      "sync": {"kind": "daemon"},
      "local_root": "/data/ya_disk",
      "state_dir": "/data/ydm/yandex",
      "config_profile": "prod"
    },
    "gdrive": {
      "provider": "google_drive",
      "inventory": {"kind": "rclone", "remote": "gdrive"},
      "sync": {"kind": "rclone", "remote": "gdrive"},
      "local_root": "/data/gdrive",
      "state_dir": "/data/ydm/gdrive",
      "config_profile": "prod",
      "native_docs": "metadata"
    }
  }
}
```

```bash
chmod 600 "$HOME/.config/ydm/targets.json"
```

Безопасный начальный режим `native_docs=metadata` учитывает Google-native
Docs/Sheets/Slides в inventory, но не пытается материализовать их как обычные
файлы.

## 3. Проверить конфигурацию без сети

```bash
cd /path/to/ydm
python3 ydm.py --format json \
  --targets-file "$HOME/.config/ydm/targets.json" \
  targets validate
```

Команда должна вернуть `"valid": true` и не создаёт remote-объекты.

## 4. Выполнить read-only remote probes

```bash
python3 ydm.py --format json \
  --targets-file "$HOME/.config/ydm/targets.json" \
  targets doctor gdrive

python3 ydm.py --format json \
  --targets-file "$HOME/.config/ydm/targets.json" \
  targets doctor yandex
```

Ожидается `"read_only": true`, а оба поля reachability — `true`.

## 5. Построить полный Google cloud inventory

```bash
python3 ydm.py --format json \
  --target gdrive \
  --targets-file "$HOME/.config/ydm/targets.json" \
  scan cloud
```

Если процесс был прерван, проверить последний scan и продолжить его:

```bash
python3 ydm.py --format json \
  --target gdrive \
  --targets-file "$HOME/.config/ydm/targets.json" \
  report scan-list --limit 3

python3 ydm.py --format json \
  --target gdrive \
  --targets-file "$HOME/.config/ydm/targets.json" \
  scan cloud --resume
```

## 6. Проверить результат и остановиться

```bash
export YDM_TARGETS_FILE="$HOME/.config/ydm/targets.json"
source /path/to/ydm/tools/aliases.sh
ydm-use-target gdrive
ydm-tree
ydm-sync-state
```

До отдельного решения о составе синхронизации **не запускать**:

- `download-apply --apply`;
- `ydm-bisync-resync --apply`;
- `ydm-bisync-run`;
- scheduler install;
- `yandex-disk start` или `yandex-disk resume`.

Следующий этап начинается с просмотра inventory и выбора папок. Для каждой
папки отдельно назначается `download_only`, `bidirectional` или отсутствие в
policy; Google-native документы экспортируются отдельным контролируемым
действием.
