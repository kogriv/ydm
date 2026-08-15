# Unified sync interface — how to use

*Дата:* 2026-08-14  
*Статус:* код написан и покрыт тестами, но не закоммичен и не проверен на живом окружении

Этот документ описывает целевой UX задачи `tasks/sync_unification`. Проверка на реальном демоне ещё не пройдена — до неё поведение может отличаться.

---

## Идея

Один набор команд для синхронизации Яндекс.Диска — независимо от окружения:

- **Ubuntu + `yandex-disk` daemon** — классический путь
- **Android/Termux + `rclone bisync`** — путь без демона

Команды одинаковые. Проект сам определяет backend или позволяет выбрать явно.

---

## Быстрый старт

### 1. Проверить, какой backend обнаружился

```bash
python3 tools/ydm_menu.py --help
python3 tools/ydm_menu.py        # интерактивное меню
```

В шапке меню будет строка:
```text
Backend: yandex-disk daemon (auto-detected)
```

или

```text
Backend: rclone (auto-detected)
```

### 2. Миграция существующих исключений (Ubuntu)

Если вы раньше управляли синхронизацией через `exclude-dirs` в `~/.config/yandex-disk/config.cfg`, перенесите их в единую policy:

```bash
python3 tools/sync_policy.py migrate --backend daemon --apply
```

Это создаст `var/sync_policy.json`, где текущие исключения станут `disabled`.

### 3. Добавить папку в синхронизацию

```bash
# Ubuntu / Android — одинаково
ydm-sync-add /Books/Math/АнГем

# Явно указать режим
ydm-sync-add --mode download_only /Books/Math/АнГем
```

На Ubuntu это уберёт `/Books/Math/АнГем` из `exclude-dirs` и перезапустит демон.

На Android это добавит путь в `.bisync.filters` и предложит `resync`.

### 4. Убрать папку из синхронизации

```bash
ydm-sync-rm /Books/Math/АнГем
```

На Ubuntu это добавит путь в `exclude-dirs` и перезапустит демон.

На Android это уберёт путь из `.bisync.filters`.

### 5. Посмотреть дерево синхронизации

```bash
ydm-tree
ydm-tree-path /Books/Math 3
```

Маркеры:

| Маркер | Значение |
|--------|----------|
| `[B]` | bidirectional sync |
| `[D]` | download-only (только rclone backend) |
| `[L]` | локально есть, в policy нет — orphan |
| `[X]` | disabled / исключено из sync |
| `[.]` | только в облаке, не в policy |

### 6. Запустить синхронизацию

На Ubuntu демон синхронизирует сам. Можно проверить статус:

```bash
yandex-disk status
```

На Android запустить bisync из меню:

```bash
ydm-menu
# 5 — Run bisync now
```

---

## Backend detection

Приоритет (от высшего к низшему):

1. CLI флаг: `--backend daemon` или `--backend rclone`
2. Env var: `YDM_BACKEND=daemon`
3. `ydm_config.json`: `"backend": "daemon"`
4. Auto-detect:
   - Если есть `yandex-disk` и `~/.config/yandex-disk/config.cfg` → `daemon`
   - Иначе если есть `rclone` remote `yandex` → `rclone`
   - Иначе ошибка

Пример явного выбора:

```bash
python3 tools/ydm_menu.py --backend rclone
```

Если на машине запущен `yandex-disk` daemon и вы выбрали `rclone`, программа спросит:

```text
yandex-disk daemon is active. Using rclone alongside it will cause conflicts.
Stop the daemon now? [Y/n]
```

При подтверждении выполнится `yandex-disk stop`.

---

## Ограничения

### `download_only` на daemon backend

Демон `yandex-disk` умеет только bidirectional sync. Если вы попробуете:

```bash
ydm-sync-add --mode download_only /Books/Math/АнГем
```

на Ubuntu, получите ошибку:

```text
download_only is not supported by the yandex-disk daemon backend.
Use --backend rclone, or switch the path to bidirectional/disabled.
```

### Cloud folder picker на daemon

Меню пункт **2. Add folder from cloud** берёт список подпапок из `monitor.db` snapshot. Если snapshot устарел, меню предложит сначала сделать cloud scan.

---

## Поддерживаемые команды

| Команда | Ubuntu daemon | Android rclone |
|---------|---------------|------------------|
| `ydm-menu` | ✅ | ✅ |
| `ydm-tree` | ✅ | ✅ |
| `ydm-tree-path` | ✅ | ✅ |
| `ydm-sync-add <path>` | ✅ | ✅ |
| `ydm-sync-add --mode download_only <path>` | ❌ | ✅ |
| `ydm-sync-rm <path>` | ✅ | ✅ |
| `ydm-scan-cloud` | ✅ | ✅ (via rclone) |
| `ydm-scan-local` | ✅ | ✅ |
| `ydm orphans` | ✅ | ✅ |

---

## Troubleshooting

### `ydm-menu` пишет "Backend: rclone" на Ubuntu

Проверьте:

```bash
which yandex-disk
ls ~/.config/yandex-disk/config.cfg
```

Если файла нет — либо установите демон, либо используйте `--backend rclone`.

### `ydm-tree` показывает `[?]` вместо `[B]`/`[X]`

Нужна миграция:

```bash
python3 tools/sync_policy.py migrate --backend daemon --apply
```

### После `ydm-sync-add` папка не появилась локально

На Ubuntu демон синхронизирует асинхронно. Проверьте:

```bash
yandex-disk status
```

На Android возможно нужен resync:

```bash
python3 tools/sync_bisync.py resync --apply
```

### Хочу вернуться к старому `sync_exclude.py`

Можно продолжать использовать его напрямую:

```bash
python3 tools/sync_exclude.py add --path /Books/Math/АнГем --apply   # исключить
python3 tools/sync_exclude.py remove --path /Books/Math/АнГем --apply # включить
```

Но тогда `var/sync_policy.json` и `ydm-tree` будут рассинхронизированы.
