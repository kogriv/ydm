# Unified sync interface

*Задача:* сделать единый CLI для управления синхронизацией Яндекс.Диска на разных окружениях: Ubuntu с штатным демоном `yandex-disk` и Android/Termux с `rclone bisync`.

## Статус

- GAP analysis: ✅ `GAP.md`
- Design: ✅ `DESIGN.md`
- Backlog: ✅ `BACKLOG.md`
- User guide: ✅ `HOW_TO_USE.md`
- Implementation: ⏳ не начата

## Проблема

В проекте YDM существуют два параллельных стека синхронизации:

1. **Daemon backend** (`yandex-disk`) для Ubuntu:
   - `~/.config/yandex-disk/config.cfg` с `exclude-dirs=`
   - `tools/sync_exclude.py`
   - `ydm.py --backend api`
   - `tools/sync_tree.py --backend api`

2. **Rclone backend** для Android/Termux:
   - `var/sync_policy.json`
   - `tools/sync_policy.py`
   - `tools/sync_bisync.py`
   - `tools/ydm_menu.py`

На Ubuntu-хосте с штатным демоном интерактивное меню `ydm-menu` не работает, `ydm-tree` показывает устаревшие/непонятные маркеры, а `ydm-sync-add` в `.bashrc` имеет перепутанную семантику (`add` = исключить из sync).

## Цель

Один и тот же интерфейс:

```bash
ydm-menu
ydm-tree
ydm-sync-add /Books/Math/АнГем
ydm-sync-rm /Books/Math/АнГем
ydm-scan-cloud
```

должен работать одинаково на обоих окружениях, автоматически выбирая правильный backend.

## Документы

- **[GAP.md](./GAP.md)** — обнаруженные проблемы и root causes
- **[DESIGN.md](./DESIGN.md)** — архитектура решения
- **[BACKLOG.md](./BACKLOG.md)** — план реализации по фазам
- **[HOW_TO_USE.md](./HOW_TO_USE.md)** — целевая инструкция для пользователя

## Ключевые решения

- `var/sync_policy.json` — единый источник истины для обоих backend.
- `bidirectional` — дефолтный режим; `download_only` доступен только на rclone backend.
- Auto-detect backend: `yandex-disk` + config.cfg → daemon; иначе rclone remote `yandex`.
- Явный override: `--backend daemon|rclone`, env `YDM_BACKEND`, `ydm_config.json`.
- При выборе `rclone` на машине с активным daemon — warning и остановка демона.

## Связанные задачи

- [`tasks/ydm_menu/`](../ydm_menu/) — интерактивное меню
- [`tasks/sync_tree/`](../sync_tree/) — policy-aware sync tree
- [`tasks/rclone_backend/`](../rclone_backend/) — rclone backend details
