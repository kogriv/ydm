# Unified sync interface — Design

*Дата:* 2026-08-14  
*Зависимости:* `ydm.py`, `tools/sync_policy.py`, `tools/sync_tree.py`, `tools/sync_bisync.py`, `tools/sync_exclude.py`, `tools/ydm_menu.py`, `tools/ydm_menu_actions.py`, `~/.bashrc`

## Principles

1. **One policy to rule them all.** `var/sync_policy.json` — единственный источник истины для того, что и как синхронизируется.
2. **Backend-agnostic CLI.** Команды `ydm-sync-add`, `ydm-sync-rm`, `ydm-tree`, `ydm-menu` работают одинаково на Ubuntu и Android.
3. **Auto-detect, explicit override.** По умолчанию backend определяется по окружению, но пользователь может указать `--backend daemon|rclone|auto`.
4. **Daemon is always bidirectional.** `yandex-disk` daemon не умеет download-only. Режим `download_only` доступен только на rclone backend.
5. **Safety first.** Выбор rclone на машине с активным daemon требует подтверждения и остановки daemon.
6. **Agent CLI preserved.** Все JSON tools (`sync_policy.py`, `sync_tree.py`, `sync_bisync.py`) остаются machine-readable.
7. **Stdlib only.** Никаких новых pip-зависимостей.

---

## Policy model

`var/sync_policy.json`:

```json
{
  "schema": "ydm_sync_policy:v1",
  "created_at": "...",
  "updated_at": "...",
  "local_root": "/data/ya_disk",
  "remote": "yandex",
  "paths": {
    "Books/Math/База": {"mode": "bidirectional"},
    "video/Матеша": {"mode": "download_only"},
    "Sample": {"mode": "disabled"}
  }
}
```

Режимы:

| Режим | Daemon backend | Rclone backend |
|-------|----------------|----------------|
| `bidirectional` | Убрать из `exclude-dirs`, restart daemon | Добавить в `.bisync.filters` |
| `download_only` | **NotSupportedError** | Добавить в `.download.filters` |
| `disabled` | Добавить в `exclude-dirs`, restart daemon | Не включать ни в один filter |

---

## Backend abstraction

Новый модуль `tools/sync_backends.py`.

```python
class SyncBackend(ABC):
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def apply_policy(self, policy: dict, *, dry_run: bool = False) -> dict: ...

    @abstractmethod
    def run_sync(self, *, dry_run: bool = False, stream: bool = False) -> dict: ...

    @abstractmethod
    def run_resync(self, *, dry_run: bool = False, stream: bool = False) -> dict: ...

    @abstractmethod
    def list_cloud_children(self, parent_path: str) -> list[str]: ...

    @abstractmethod
    def run_cloud_scan(self, path: str) -> dict: ...

    @abstractmethod
    def status(self) -> dict: ...
```

### `DaemonBackend`

```python
class DaemonBackend(SyncBackend):
    def __init__(self, config_path: str, db_path: str, local_root: str):
        self.config_path = config_path  # ~/.config/yandex-disk/config.cfg
        self.db_path = db_path
        self.local_root = local_root
```

`apply_policy(policy)`:
1. Прочитать текущий `exclude-dirs` из `config.cfg`
2. Собрать список `disabled` путей из policy
3. Обновить `exclude-dirs` = всё, что `disabled` на любой глубине
4. Поддерживать формат `dir`, `dir/subdir`
5. Перезапустить демон через `yandex-disk stop && yandex-disk start`
6. Вернуть JSON result

`run_cloud_scan(path)`:
- `python3 ydm.py --db-path {db_path} --backend api scan cloud --path {path} --progress`

`list_cloud_children(parent_path)`:
- Через `monitor.db` snapshot: `SELECT name FROM files WHERE parent_path = ? AND type = 'dir'`
- Если snapshot устарел / пуст — вернуть `[]` + warning

`run_sync()` / `run_resync()`:
- Для daemon backend синхронизация запускается самим демоном.
- `run_sync()` возвращает статус демона (`yandex-disk status`)
- `run_resync()` — **NotSupportedError** (у демона нет такого понятия)

`download_only`:
- `apply_policy()` падает с понятным сообщением:
  ```
  download_only is not supported by the yandex-disk daemon backend.
  Use --backend rclone, or switch the path to bidirectional/disabled.
  ```

### `RcloneBackend`

```python
class RcloneBackend(SyncBackend):
    def __init__(self, remote: str, db_path: str, local_root: str,
                 policy_path: str, bisync_filter_path: str):
        self.remote = remote
        self.db_path = db_path
        self.local_root = local_root
        self.policy_path = policy_path
        self.bisync_filter_path = bisync_filter_path
```

`apply_policy(policy)`:
- Вызвать `sync_policy.render_filters()` → записать `.bisync.filters` и `.download.filters`
- (legacy `.filters` оставить для совместимости? опционально)

`run_cloud_scan(path)`:
- `python3 ydm.py --db-path {db_path} --backend rclone scan cloud --path {path} --progress`

`list_cloud_children(parent_path)`:
- `rclone lsf {remote}:{parent_path} --dirs-only --max-depth 1`

`run_sync()`:
- `sync_bisync.py run --apply`

`run_resync()`:
- `sync_bisync.py resync --apply`

---

## Backend detection

```python
def detect_backend(
    *,
    explicit: str | None = None,
    config: dict,
    db_path: str,
    local_root: str,
    policy_path: str,
    bisync_filter_path: str,
) -> SyncBackend:
```

Приоритет:
1. `explicit` (`--backend daemon|rclone|auto`) если не `auto`
2. Env var `YDM_BACKEND`
3. `config.get("backend")` из `ydm_config.json`
4. Auto-detect:
   - Если `shutil.which("yandex-disk")` и `os.path.exists(expanduser("~/.config/yandex-disk/config.cfg"))` → `daemon`
   - Иначе если rclone remote "yandex" существует → `rclone`
   - Иначе raise `RuntimeError`

При выборе `rclone`:
- Проверить, запущен ли `yandex-disk` (`yandex-disk status` returns success)
- Если да — warning + prompt:
  ```
  yandex-disk daemon is active. Using rclone backend alongside it will cause conflicts.
  Stop the daemon now? [Y/n]
  ```
- Если подтверждено — `yandex-disk stop`, затем продолжить
- Если отказ — abort

---

## Changes by file

### `tools/sync_backends.py` (new)

- `SyncBackend` ABC
- `DaemonBackend`
- `RcloneBackend`
- `detect_backend()`
- `stop_yandex_disk_if_active()` helper

### `tools/sync_policy.py`

- Добавить `--backend {daemon,rclone,auto}`
- `add_policy_path()` — default mode `bidirectional` (уже так)
- `apply_policy()` вызывает backend-специфичный `apply_policy()`
- Новый subcommand `migrate --backend daemon --apply`:
  - Читает `exclude-dirs` из `~/.config/yandex-disk/config.cfg`
  - Создаёт `var/sync_policy.json` с `disabled` для каждого пути
  - Не трогает `config.cfg` до следующего `apply`
- `download_only` на daemon backend → clear error

### `tools/sync_tree.py`

- `--backend api` переименовать в `--backend daemon` для консистентности? Или сохранить `api` как alias.
- `--use-policy` по умолчанию `true` для всех backend, если `var/sync_policy.json` существует.
- Для daemon backend membership определяется по policy:
  - `bidirectional` → `[B]`
  - `download_only` → если на daemon, показать `[?]` или `[X]` с warning
  - `disabled` → `[X]`
  - не в policy, но в `exclude-dirs` → `[X]`
  - локально есть, не в policy → `[L]`
- Для rclone backend оставить текущее поведение.

### `tools/ydm_menu_config.py`

- Добавить поле `backend: str` в `MenuConfig`
- `from_env_and_args()` передаёт backend в detect_backend()

### `tools/ydm_menu_actions.py`

- Заменить прямые вызовы `rclone` / `sync_bisync` / `sync_tree --backend rclone` на backend methods
- `cloud_list_dirs(cfg, parent)` → `cfg.backend.list_cloud_children(parent)`
- `run_cloud_scan(cfg, path)` → `cfg.backend.run_cloud_scan(path)`
- `action_bisync_run()` → `cfg.backend.run_sync()`
- `action_resync()` → `cfg.backend.run_resync()`
- `run_sync_tree()` → `sync_tree.py` с правильным backend
- `action_add()` / `action_remove()` — через `sync_policy.py add/remove` + backend apply

### `tools/ydm_menu.py`

- Добавить `--backend {daemon,rclone,auto}`
- На старте: detect backend + warning если rclone на машине с daemon
- Все screen handlers используют backend-agnostic actions

### `.bashrc`

Обновить aliases:

```bash
alias ydm-menu="python3 $YDM_ROOT/tools/ydm_menu.py --db-path $YDM_DB --local-root $YDM_LOCAL_ROOT"

ydm-sync-add() {
  python3 "$YDM_ROOT/tools/sync_policy.py" add \
    --db-path "$YDM_DB" \
    --local-root "$YDM_LOCAL_ROOT" \
    --mode "${2:-bidirectional}" \
    --apply --path "$1"
}

ydm-sync-rm() {
  python3 "$YDM_ROOT/tools/sync_policy.py" add \
    --db-path "$YDM_DB" \
    --local-root "$YDM_LOCAL_ROOT" \
    --mode disabled \
    --apply --path "$1"
}

alias ydm-tree="python3 $YDM_ROOT/tools/sync_tree.py --db-path $YDM_DB --format text --text-tree"
```

При желании `ydm-sync-add` может быть wrapper над `ydm_menu.py --non-interactive add`, но проще оставить `sync_policy.py`.

### `ydm_config.json`

- Добавить опциональное поле `backend` в профили

```json
{
  "prod": {
    "cloud_batch_size": 500,
    "backend": "auto"
  },
  "test": {
    "checkpoint_files_threshold": 500,
    "backend": "auto"
  }
}
```

### `tests/`

- `tests/test_sync_backends.py` — unit tests для backend abstraction
- Обновить `tests/test_sync_tree.py` — policy overlay для daemon backend
- Обновить `tests/test_ydm_menu.py` — backend mock/detection

### CI

- `.github/workflows/ci.yml` — добавить py_compile `tools/sync_backends.py`
- Smoke test: `sync_policy.py --help`, `ydm_menu.py --help`, `sync_tree.py --help`

---

## Migration path

### Для существующего Ubuntu-окружения

1. Запустить миграцию:
   ```bash
   python3 tools/sync_policy.py migrate --backend daemon --apply
   ```
   Создаёт `var/sync_policy.json` из текущих `exclude-dirs`.

2. После этого `ydm-sync-add /path` и `ydm-sync-rm /path` работают через policy.

3. Первый `ydm-sync-add --apply` перезапишет `config.cfg` по новому policy.

### Для нового пользователя

1. Запустить `ydm-menu` → auto-detect daemon → создать пустую policy.
2. Добавить/убирать папки через меню.

### Для Android

Поведение не меняется, если remote `yandex` настроен. Можно оставить `backend: rclone` в config.

---

## UX flows after unification

### Ubuntu / daemon

```bash
$ ydm-menu
Backend: yandex-disk daemon (auto-detected)

YDM Sync
──────────────────────────────────────
Status: OK        last run: daemon active
Lock: no

Synced: brtn[B]  pro[B]  +3 more

 1  Show sync tree
 2  Add folder from cloud
 3  Add LOCAL folder to sync
 4  Remove folder from sync
 5  Run daemon sync (restart + status)
 6  Cloud scan (update snapshot)
 7  Detailed status
 8  Help
 q  Quit

> 2
Cloud parent:
 1  /Books/Math
 2  /pro
 3  /video
 4  Custom
> 1

Subfolders:
 1  База
 2  АнГем
 3  Линал

Pick: 2
Mode: 1=bidirectional  2=download-only (not available)  0=cancel
> 1

Added /Books/Math/АнГем as bidirectional
Restarting yandex-disk daemon...
Done.
```

### Android / rclone

```bash
$ ydm-menu
Backend: rclone (auto-detected)

> 3
Local folders not in sync:
 1  Books/Math/АнГем
Pick: 1
Mode: 1=bidirectional  2=download-only  0=cancel
> 1

Filters changed. Run bisync resync now? [Y/n]
```

---

## Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Миграция `exclude-dirs` в policy перепишет config.cfg непредсказуемо | Миграция только создаёт policy; первый apply — dry-run by default |
| Daemon restart прерывает текущую синхронизацию | Apply с `--no-restart` option для advanced users |
| `download_only` на daemon ломает UX | Clear error + suggest rclone |
| rclone backend на машине с daemon = конфликт | Explicit warning + stop daemon prompt |
| Тесты зависят от реального `yandex-disk` | Backend classes mockable; CI не запускает daemon |
| Дублирование `exclude-dirs` и policy | `DaemonBackend.apply_policy()` canonicalizes exclude-dirs each time |

---

## Decision log

| Decision | Rationale |
|----------|-----------|
| Policy file `var/sync_policy.json` for both backends | Single source of truth for add/remove/tree |
| Daemon backend only bidirectional/disabled | Matches yandex-disk daemon capabilities |
| `ydm-sync-add` default mode = bidirectional | Most common intent |
| `ydm-sync-rm` = set mode disabled | Symmetric with add; clear semantics |
| Auto-detect prefers daemon over rclone | Ubuntu is primary dev environment; safer default |
| Explicit `--backend rclone` stops daemon | Prevents data corruption from dual sync |
| Keep `sync_exclude.py` as backend detail | Don't break existing users; internal API |
