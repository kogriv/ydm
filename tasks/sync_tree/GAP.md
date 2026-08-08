# Sync Tree v2 — Gap Analysis

*Дата:* 2026-08-08
*Статус:* зафиксировано по инциденту «`ydm-tree` не показывает дерево / не
отражает реальный sync»

## Контекст

Исторически `ydm-tree` показывал облачное дерево с метками `[S]` / `[P]` /
`[-]` — что включено в sync, частично, или исключено. Это работало при
модели `exclude-dirs` + полном cloud scan.

С июля 2026 основной путь на этом устройстве — **policy-aware rclone**:

```text
var/sync_policy.json          ← source of truth (режимы path → mode)
  ↓ render-filters
ya_disk.download.filters      ← materialize (copy)
ya_disk.bisync.filters        ← scheduled bisync
```

`ydm-sync-state` читает policy + bisync state, но **`ydm-tree` — нет**.

## Симптомы (user-facing)

| Симптом | Пример |
|---------|--------|
| Команда «ничего не показывает» | `ydm-tree` → только `[-] Math 100.0%`, без веток |
| Ошибка без понятного текста | `python3: can't open file '/tools/sync_tree.py'` |
| Метки не совпадают с `ydm-sync-state` | `Books/Math/База` bidirectional, но в дереве `[-]` |
| Локальные «осиротевшие» папки не видны | `Books/Math/АнГем` на диске (~393 MB), но не в policy |
| Долгий запуск | каждый `ydm-tree` по умолчанию делает local scan (~4+ сек) |

## Root causes

### G1. Алиас `ydm-tree` читает legacy filter, не policy

```bash
# ~/.bashrc — ydm-tree не передаёт --filter-path
python3 "$YDM_DIR/tools/sync_tree.py" ... --backend rclone
# default → /sdcard/Download/ya_disk.filters  (устаревший)
```

Актуальный bisync filter: `ya_disk.bisync.filters` (генерируется из policy).

Legacy `.filters` содержит старые пути (`pro/agents/**`), не совпадает с policy.

### G2. `sync_tree.py` не знает про `sync_policy.json`

Membership определяется только по include-строкам filter-file. Режимы
(`bidirectional` / `download_only` / `disabled`) и причины риска (`reason`,
`risk_scan_id`) **не отображаются**.

Пользователь видит два разных интерфейса:

- `ydm-sync-state` — flat list bidirectional/download_only
- `ydm-tree` — дерево по filter-file без режимов

### G3. Пустое дерево из-за устаревшего / частичного cloud snapshot

`build_composite_scan()` при отсутствии «свежих» full scan (окно 2 дня) падает
в fallback → **последний cloud scan** (#886, partial `agents_week`, 26 files).

Частичный scan:

- не содержит `dir` rows для `/Books/Math`
- `fetch_child_dirs()` возвращает `[]` → дерево пустое

Последний «полный» scan с корневыми папками — #1 от 2026-07-10 (32 root dirs).

**Workaround:** `ydm-scan-cloud /Books/Math` (запущен пользователем 2026-08-08,
scan #2426).

### G4. Несогласованность `parent_path` в БД

| Источник | Формат `parent_path` для корневых детей |
|----------|----------------------------------------|
| API scan #1 | `""` + name `Books` |
| rclone partial scan #866 | `/Books/Math/АнГем` (файлы), dir rows отсутствуют |

`fetch_child_dirs()` ищет `type='dir'` с `parent_path = normalize_db_parent_path(path)`.
При rclone partial scans dir entries могут отсутствовать → дерево не строится
даже при наличии файлов.

### G5. Schema `sync_tree:v1` не покрывает policy-модель

Текущие поля node:

```json
{
  "path": "/Books/Math/База",
  "sync_status": "full|partial|excluded",
  "sync_percent": 100.0
}
```

Нет:

- `policy_mode` (`bidirectional` | `download_only` | `disabled` | null)
- `local_state` (`materialized` | `orphan` | `missing`)
- `in_policy` (bool)
- `risk` (optional)

### G6. Метки `[S]`/`[P]`/`[-]` семантически устарели

| Метка v1 | Значение v1 | Проблема |
|----------|-------------|----------|
| `[S]` | included in filter | не различает bidirectional vs download_only |
| `[P]` | partial (descendant synced) | при малой `--depth` родитель остаётся `[-]` |
| `[-]` | excluded | не отличает «только облако» от «local orphan» |

Нужны отдельные оси: **policy mode** × **local presence** × **cloud coverage**.

### G7. UX: нет legend, нет snapshot health warning

Text header показывает `config_path`, `collapsed`, `local_scan_started`, но:

- не предупреждает о stale snapshot
- не показывает policy summary
- не объясняет метки

### G8. `YDM_DIR` unset → cryptic error

Если shell не загрузил `~/.bashrc`, `$YDM_DIR` пуст → путь `/tools/sync_tree.py`.
Exit code может быть 0 при ошибке в function wrapper.

## Что уже работает (не ломать)

- `--backend api` + `exclude-dirs` — legacy path для amd64 + daemon
- composite snapshot для file counts / sync_percent
- collapse mode (`--collapse-synced`) — полезен при большом дереве
- JSON output для автomation/AI (`schema: sync_tree:v1`)

## Non-goblems (осознанно вне scope v2)

- Полная замена `ydm-sync-state` одной командой (v2 дополняет, не заменяет)
- Interactive TUI / curses
- Управление policy из дерева (`add`/`remove`) — остаётся в `ydm-sync-add/rm`
- Решение проблемы Android forbidden chars (уже в `sync_policy.py` / repair tools)

## Acceptance: gap считается закрытым когда

1. `ydm-tree-path /Books/Math 3` после partial cloud scan показывает детей с
   корректными policy-метками.
2. Папка в policy как `bidirectional` помечена иначе, чем `download_only`.
3. Local orphan (`АнГем`) видна как отдельное состояние, не как `[S]`.
4. При stale snapshot — явное предупреждение в header, не пустое дерево без
   объяснения.
5. `ydm-tree` и `ydm-sync-state` согласованы по списку bidirectional paths.
