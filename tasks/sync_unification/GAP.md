# Unified sync interface — Gap Analysis

*Дата:* 2026-08-14  
*Статус:* зафиксировано после анализа работы проекта на Ubuntu-хосте с штатным демоном `yandex-disk`

## Контекст

Проект YDM последние месяцы развивался преимущественно под окружение Android/Termux/proot-Debian, где штатный демон `yandex-disk` не работает. В этом окружении синхронизация реализована через `rclone bisync` с использованием policy-aware слоя:

- `var/sync_policy.json` — единая политика путей
- `tools/sync_policy.py` — управление политикой
- `tools/sync_bisync.py` — bidirectional sync через `rclone bisync`
- `tools/sync_filters.py` — legacy one-way `rclone copy`
- `tools/ydm_menu.py` — интерактивное меню

Параллельно для классического Ubuntu-окружения сохранился первоначальный путь:

- `~/.config/yandex-disk/config.cfg` с `exclude-dirs=`
- `tools/sync_exclude.py` — управление исключениями
- `tools/sync_tree.py --backend api` — дерево из snapshot + exclude-dirs
- `ydm.py scan cloud --backend api` — сканирование облака

В результате в репозитории существуют **два независимых мира синхронизации**, не объединённые общей абстракцией.

---

## Симптомы (user-facing)

| Ситуация на Ubuntu-хосте | Текущее поведение | Боль |
|--------------------------|-------------------|------|
| Пользователь запускает `ydm-menu` | Меню пытается работать через `rclone`, remote `yandex` не настроен — ошибка | Интерактивный UI не работает на основной платформе разработки |
| `ydm-tree` | Показывает маркеры `[?]` вместо `[B]`/`[D]`/`[L]`/`[X]` | Нет policy overlay для daemon backend |
| `ydm-sync-add /Books/Math/АнГем` | В `.bashrc` вызывает `sync_exclude.py add`, который **исключает** папку из sync | Семантика перепутана: add = disable sync |
| Добавить папку в синхронизацию демона | Нужно вручную редактировать `exclude-dirs` или использовать `sync_exclude.py remove` | Нет человекочитаемого CLI |
| Понять, что синкается | `yandex-disk status` + `sync_exclude.py list` + устаревший `ydm-tree` | Нет единого отчёта |
| Использовать `download_only` | Невозможно: демон умеет только bidirectional | Режим есть в policy, но не применим на daemon backend |
| Выбрать backend явно | Нет такой опции | Пользователь не может сказать «здесь используй rclone» |

---

## Root causes

### G1. Два backend-стека без общей абстракции

```text
Ubuntu path:
  yandex-disk daemon  →  config.cfg exclude-dirs  →  sync_exclude.py

Android path:
  rclone              →  .bisync.filters / .download.filters  →  sync_policy.py / sync_bisync.py
```

Нет класса/модуля, который бы скрывал разницу между этими backend.

### G2. `ydm_menu.py` жёстко завязан на rclone

`ydm_menu_actions.py`:
- `cloud_list_dirs()` использует `rclone lsf`
- `run_cloud_scan()` запускает `ydm.py scan cloud --backend rclone`
- `run_sync_tree()` запускает `sync_tree.py --backend rclone`
- `action_bisync_run()` / `action_resync()` используют `sync_bisync.py`

На Ubuntu с настроенным демоном remote `yandex` отсутствует, поэтому меню не работает.

### G3. Policy overlay не работает для daemon backend

`sync_tree.py`:
- `--use-policy` по умолчанию включён только для `--backend rclone`
- Для `--backend api` дерево строится из `exclude-dirs`, но маркеры `[B]`/`[D]`/`[L]`/`[X]` не применяются
- Файл `var/sync_policy.json` игнорируется

### G4. Семантика `ydm-sync-add` / `ydm-sync-rm` перепутана

В `~/.bashrc`:
```bash
ydm-sync-add() { sync_exclude.py add --apply --path "$1" }
ydm-sync-rm()  { sync_exclude.py remove --apply --path "$1" }
```

Но в `sync_exclude.py`:
- `add` — добавляет папку в `exclude-dirs` (отключает sync)
- `remove` — убирает папку из `exclude-dirs` (включает sync)

Получается:
- `ydm-sync-add /foo` → `/foo` **перестаёт** синхронизироваться
- `ydm-sync-rm /foo` → `/foo` **начинает** синхронизироваться

### G5. Нет auto-detect backend

Проект не анализирует окружение при старте. Пользователь должен сам знать, какой backend использовать. В результате:
- на Ubuntu по умолчанию пытается использоваться rclone-stack
- на Android невозможно accidentally использовать daemon stack

### G6. `download_only` не реализован для daemon backend

В `sync_policy.json` режим `download_only` предполагается для `rclone copy`/`.download.filters`. Для `yandex-disk` демона такого режима не существует — демон всегда bidirectional. Сейчас это не обработано.

### G7. Нет защиты от конфликта daemon + rclone

Если пользователь на Ubuntu явно выберет `--backend rclone`, `rclone bisync` может пытаться синхронизировать те же папки, что и активный `yandex-disk` daemon. Это приведёт к конфликтам и потере данных. Никакого warning или автоматической остановки демона нет.

### G8. Дублирование логики в bash aliases и Python tools

Критичные flows разбиты:
- `~/.bashrc` знает про `YDM_DB`, `YDM_LOCAL_ROOT`, парсит JSON
- `ydm_menu_config.py` тоже знает про те же переменные
- `sync_policy.py`, `sync_exclude.py`, `sync_filters.py` имеют пересекающуюся функциональность

---

## Что уже хорошо (не ломать)

| Asset | Почему сохранить |
|-------|------------------|
| `var/sync_policy.json` schema `ydm_sync_policy:v1` | Удачная единая модель путей |
| `sync_policy.py inspect/add/remove/render-filters` | Хороший risk-aware policy layer |
| `sync_tree.py` v2 schema | Маркеры и policy overlay работают для rclone |
| `ydm_menu.py` REPL и screens | Проработанный UX для человека |
| `sync_bisync.py` | Надёжный `rclone bisync` wrapper с check-access |
| `sync_exclude.py` | Работает как backend-деталь для daemon |

---

## Acceptance: gap закрыт когда

1. Один и тот же CLI (`ydm-menu`, `ydm-sync-add`, `ydm-sync-rm`, `ydm-tree`) работает на Ubuntu и Android.
2. Backend выбирается автоматически, но может быть переопределён явно.
3. На Ubuntu с daemon: `ydm-sync-add /path` **включает** синхронизацию, `ydm-sync-rm /path` **выключает**.
4. `ydm-tree` показывает `[B]`/`[D]`/`[L]`/`[X]` для обоих backend.
5. `download_only` на daemon backend либо запрещён с понятной ошибкой, либо честно эмулируется.
6. Выбор `rclone` на машине с активным daemon требует подтверждения и останавливает daemon.
7. Все существующие JSON/agent tools продолжают работать.
8. Есть документы: GAP, DESIGN, BACKLOG, HOW_TO_USE.

---

## Open questions (resolved in DESIGN)

1. Как backend-agnostic CLI применяет policy к `exclude-dirs`? → `DaemonBackend.apply_policy()`
2. Что делать с `download_only` на daemon? → `NotSupportedError` с понятным сообщением
3. Как auto-detect выбирает между daemon и rclone? → `yandex-disk` + config.cfg имеет приоритет
4. Как `ydm_menu.py` получает список подпапок без `rclone lsf` на Ubuntu? → через `monitor.db` snapshot
5. Как мигрировать существующие `exclude-dirs` в policy? → explicit `sync_policy.py migrate --backend daemon --apply`
