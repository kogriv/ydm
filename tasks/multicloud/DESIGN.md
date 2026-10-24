# Multi-cloud targets — Design

*Дата:* 2026-10-23
*Scope:* Yandex Disk остаётся совместимым по поведению; Google Drive получает
эквивалентные inventory, diff и policy workflows через rclone там, где
семантика провайдера это позволяет.

## 1. Контекст

В текущем коде смешаны три разных выбора:

1. **Provider** — Яндекс.Диск или Google Drive.
2. **Inventory transport** — прямой Yandex REST API или `rclone lsjson`.
3. **Sync engine** — официальный `yandex-disk` daemon или `rclone bisync`.

`--backend api|rclone` частично отвечает сразу на все три вопроса. Auto-detect
выбирает daemon только потому, что бинарник и config существуют на машине, а
общие default paths ведут к одному `monitor.db`, одной policy и одному набору
filter/state файлов. Для второй облачной цели этого недостаточно.

Уже подтверждено на настроенном Google Drive remote:

- `rclone about`, `lsd` и `lsjson` работают;
- `lsjson --metadata` отдаёт стабильный `ID` и `MimeType`;
- Google-native документы могут выглядеть как `.xlsx`/`.docx`, но иметь
  `Size = -1` и не иметь hashes;
- rclone transport сам по себе provider-agnostic, а модель данных YDM — пока
  нет.

## 2. Цели

1. Один код и один CLI для нескольких независимо настроенных cloud targets.
2. Полная изоляция БД, policy, filters, locks, logs и scheduler state.
3. Cloud object identity, не зависящая от уникальности пути.
4. Read-only Google Drive inventory с существующими отчётами YDM.
5. Выборочная синхронизация с режимами `download_only`, `bidirectional` и
   `disabled`, если target capability это разрешает.
6. Нулевое изменение текущего Yandex поведения при отсутствии `--target`.
7. Конфигурация и тесты, которые можно безопасно публиковать без имён реальных
   документов, object IDs, токенов и снимков аккаунта.

## 3. Не цели первой версии

- Полная локальная копия Google Drive.
- Bidirectional round-trip Google Docs/Sheets/Slides через экспортированные
  Office-файлы.
- Синхронизация Shared Drives в первом milestone.
- Унификация provider-specific revision, trash и restore API под фиктивным
  «общим знаменателем».
- Переименование проекта или отдельный fork кода под названием GDM.

## 4. Термины и границы

### Target

Именованная эксплуатационная цель: один provider account/root, один inventory
transport, один sync engine и один state directory.

### Provider

Семантика облака: object identity, duplicate names, native documents, shortcuts,
trash, revisions, server-side operations.

### Inventory backend

Только перечисляет и читает метаданные. Не управляет локальной синхронизацией.

### Sync engine

Применяет policy и переносит содержимое: Yandex daemon или rclone.

Разделение обязательно: Google Drive и Yandex Disk могут использовать один
rclone transport, но не обладают одинаковыми возможностями.

## 5. Конфигурация targets

Текущий `--config-profile prod|test` — профиль tuning-параметров сканирования.
Новую сущность нельзя называть config profile без миграционной неоднозначности,
поэтому публичное имя — `target`.

Новый файл `ydm_targets.json` (или путь из `YDM_TARGETS_FILE`):

```json
{
  "schema": "ydm_targets:v1",
  "default_target": "yandex",
  "targets": {
    "yandex": {
      "provider": "yandex",
      "inventory": {"kind": "yandex_api"},
      "sync": {"kind": "daemon"},
      "local_root": "/srv/cloud-mirrors/yandex",
      "state_dir": "/var/lib/ydm/yandex",
      "config_profile": "prod"
    },
    "gdrive": {
      "provider": "google_drive",
      "inventory": {"kind": "rclone", "remote": "gdrive"},
      "sync": {"kind": "rclone", "remote": "gdrive"},
      "local_root": "/srv/cloud-mirrors/google",
      "state_dir": "/var/lib/ydm/gdrive",
      "config_profile": "prod",
      "native_docs": "metadata"
    }
  }
}
```

Реальный targets-файл host-specific и не должен находиться в Git. В репозитории
лежит только обезличенный `ydm_targets.example.json`.

### Производные пути

Из `state_dir` однозначно выводятся:

```text
monitor.db
sync_policy.json
download.filters
bisync.filters
bisync_state.json
bisync.log
rename_state.json
locks/
```

CLI override отдельного пути остаётся для тестов и восстановления, но обычная
эксплуатация не должна собирать target из семи независимых флагов.

### Resolution order

1. `--target NAME`.
2. `YDM_TARGET`.
3. `default_target`.
4. Legacy mode, если targets-файл отсутствует.

Legacy mode сохраняет сегодняшние defaults и является обязательным regression
contract для существующих установок.

## 6. Capability contract

Provider и transport публикуют возможности, а команды проверяют их до preview
или apply:

```python
@dataclass(frozen=True)
class Capabilities:
    stable_object_id: bool
    duplicate_sibling_names: bool
    content_hashes: frozenset[str]
    native_documents: bool
    shortcuts: bool
    revisions: bool
    trash_listing: bool
    server_side_move: bool
    bidirectional_sync: bool
```

Команда не должна угадывать provider по имени remote. Unsupported capability
возвращает структурированную ошибку, а не пустой успешный результат.

Примеры:

- `cloud_delta changes` требует `revisions`; Google v1 отвечает
  `unsupported_capability`.
- hash-duplicate report работает только по строкам с реальным content hash и
  отдельно считает `unhashable`.
- bidirectional policy не принимает native document или path collision.

## 7. Inventory interface

Нормализованная запись должна сохранять данные провайдера, а не уменьшать их до
Yandex-shaped словаря:

```python
@dataclass
class CloudItem:
    object_id: str
    parent_id: str | None
    parent_path: str
    name: str
    item_type: Literal["file", "dir", "shortcut"]
    size: int | None
    hash_name: str | None
    content_hash: str | None
    mime_type: str | None
    created: str | None
    modified: str | None
    is_native: bool
    metadata: dict
```

`RcloneClient` обязан сохранять как минимум `ID`, `MimeType`, `Metadata` и все
доступные hashes. Отсутствующий hash — факт, а не пустая MD5.

## 8. Схема хранения v2

Каждый target использует отдельную БД. Это предотвращает случайное смешение
сканов и оставляет отчёты простыми. Схема всё равно хранит source metadata,
чтобы БД была самодокументируемой.

В `scans` добавляются:

```text
target_name
provider
inventory_kind
remote_name
schema_version
```

В `files` добавляются:

```text
object_id
parent_id
mime_type
hash_name
content_hash
is_native
is_shortcut
metadata_json
```

Существующее поле `md5` временно сохраняется как compatibility projection для
строк, где `hash_name = md5`.

### Индексы идентичности

Текущий unique index `(scan_id, parent_path, name)` непригоден для Google
Drive. Его заменяют два partial indexes:

```sql
UNIQUE(scan_id, object_id)
  WHERE object_id IS NOT NULL;

UNIQUE(scan_id, parent_path, name)
  WHERE object_id IS NULL;
```

Cloud rows обязаны иметь `object_id`, если provider заявляет
`stable_object_id`. Локальные rows продолжают идентифицироваться путём.

### Миграция

- schema migration транзакционная и идемпотентная;
- существующие Yandex rows получают `object_id = NULL` и продолжают работать по
  legacy partial index;
- новый Yandex scan через REST или rclone начинает сохранять provider ID, если
  transport его отдаёт;
- перед удалением старого index создаётся backup и выполняется integrity check;
- migration тестируется на копии реальной формы schema, но с синтетическими
  данными.

## 9. Diff semantics

Нельзя использовать один критерий для всех объектов.

### Обычные файлы

1. Сопоставление cloud/local по нормализованному relative path.
2. Если доступны hashes одного типа — сравнение по hash.
3. Иначе size + modified с явной пометкой `weak_comparison`.

### Duplicate sibling names

Два cloud object с одинаковым `parent_path/name` сохраняются как две строки.
Для локального filesystem это `materialization_conflict`: обе копии нельзя
без потери идентичности представить одним путём. Такой path запрещён для sync,
но остаётся видимым в inventory и отчёте.

### Google-native documents

Режимы target:

- `metadata` — хранить объект, ID, MIME и modified; не материализовать;
- `export` — отдельный download-only artifact с записанным export format и
  source object ID;
- `skip` — не включать в inventory только по явному выбору пользователя.

Default — `metadata`. Exported artifact никогда автоматически не становится
bidirectional: локальный `.xlsx` не эквивалентен исходной Google Sheet.

### Shortcuts

Shortcut хранится отдельным объектом. V1 не следует за ним при sync и не
выдаёт target content за ребёнка shortcut path. Follow semantics остаётся
отдельной capability/phase.

## 10. Policy и синхронизация

Policy schema v2 привязана к target:

```json
{
  "schema": "ydm_sync_policy:v2",
  "target": "gdrive",
  "provider": "google_drive",
  "remote": "gdrive",
  "local_root": "/srv/cloud-mirrors/google",
  "paths": {
    "Projects": {"mode": "download_only"},
    "Archive/Binary": {"mode": "bidirectional"}
  }
}
```

### Modes

| Mode | Google ordinary file | Google-native | Yandex daemon |
|---|---|---|---|
| `disabled` | да | да | да |
| `download_only` | да | metadata/export | нет |
| `bidirectional` | после safety gate | запрещён | да |

### Apply sequence

1. Resolve target and acquire target-scoped lock.
2. Validate policy belongs to this target, remote and local root.
3. Refresh/validate cloud inventory for affected paths.
4. Reject collisions, native docs in bidirectional scope and overlapping local
   roots.
5. Render target-scoped filters.
6. Show dry-run plan.
7. Apply only after explicit `--apply` or interactive confirmation.
8. Store result and hashes in target state.

`--max-delete`, access marker, filter-hash/resync gate and rename preflight from
the current rclone stack remain mandatory.

## 11. CLI и меню

Global option:

```text
--target NAME
```

Target отображается во всех text headers и JSON envelopes. JSON schema output
получает поля `target`, `provider`, `inventory_kind`, `sync_kind`.

Menu start screen сначала выбирает target, если default не задан. После выбора
все actions получают immutable `TargetContext`; повторный auto-detect внутри
action запрещён.

Команды status должны различать:

- provider reachable;
- inventory fresh/stale;
- sync engine configured/running;
- resync needed;
- unsupported provider capability.

## 12. Изоляция и безопасность

### Runtime

- Никаких общих state files между targets.
- Lock key включает target и canonical local root.
- Два targets с одинаковым или вложенным `local_root` не проходят validation.
- Активный Yandex daemon блокирует только rclone sync на пересекающемся root,
  а не любой rclone target на хосте.
- Live validation начинается read-only; первые apply идут на disposable folder.

### Репозиторий

- Реальные targets-файлы, OAuth, `rclone.conf`, DB, logs и inventories не
  коммитятся даже в private Git.
- Fixtures генерируются и используют вымышленные names/IDs.
- Captured API output проходит sanitizer или заменяется hand-written fixture.
- Перед публикацией проверяются diff, commit messages и untracked files.
- Generic core разрабатывается отдельными publishable commits; private deploy
  overlay никогда не merge-ится обратно в publishable branch.

## 13. Тестовая стратегия

1. **Unit:** target resolver, capabilities, schema migration, item
   normalization, diff classification.
2. **Synthetic filesystem remote:** существующий sync bench расширяется двумя
   targets и доказывает изоляцию state/locks.
3. **Fake Google inventory:** duplicate names, native doc, shortcut, missing
   hash, same path with different IDs.
4. **CLI subprocess:** `--target` проходит через scan/report/tree/menu без
   обращения к default target.
5. **Mutation checks:** убрать object ID, перепутать state dirs, разрешить
   native bidirectional — тесты обязаны падать.
6. **Live read-only:** `about`, shallow scan, status и diff на отдельной
   временной DB.
7. **Disposable apply:** отдельная тестовая папка в облаке и отдельный local
   root; production tree не используется для приёмки.

## 14. Совместимость и rollout

1. Без targets-файла всё работает как до изменений.
2. `ydm targets validate` проверяет конфигурацию без сети и без writes.
3. `ydm targets doctor --target gdrive` выполняет read-only capability probe.
4. Yandex target переводится на новую модель первым, но через regression tests,
   без смены runtime defaults.
5. Google target включается сначала inventory-only.
6. Download-only и bidirectional открываются отдельными feature gates.

## 15. Отвергнутые варианты

### Отдельный GDM fork

Дублирует Analyzer, storage и safety fixes; два проекта быстро расходятся.

### Просто передавать `--remote gdrive`

Оставляет общие DB/policy/state, теряет object ID и неверно трактует native
documents.

### Одна общая БД для всех providers

Возможна теоретически, но увеличивает blast radius и требует переделать каждый
запрос Analyzer. Отдельная DB на target проще и безопаснее.

### Сразу включить bisync

Read-only модель ещё не умеет выразить duplicate names и native documents;
первый sync превратил бы неизвестность в удаление или перезапись.

## 16. Решения, отложенные до измерения

- Поддержка Shared Drives и выбор root по provider folder ID.
- Export formats для каждого Google-native MIME type.
- Нужно ли сохранять весь rclone metadata JSON или allowlist полей.
- Provider delta API для Google после стабильного полного scan.
- Публичное переименование YDM; оно не блокирует архитектуру targets.
