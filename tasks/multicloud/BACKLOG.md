# Multi-cloud targets — Backlog

*Дата:* 2026-10-23  
*Дизайн:* [`DESIGN.md`](./DESIGN.md)  
*Scope:* добавить Google Drive как изолированный target, не меняя существующее
Yandex поведение и не допуская попадания live account data в публикуемый Git.

Порядок фаз намеренно идёт от read-only к apply. Следующая фаза не открывает
write-path, пока предыдущая не умеет корректно представить данные.

## Phase 0 — Documentation ✅

| ID | Task | Status | Acceptance |
|---|---|---|---|
| 0.1 | Создать workstream README | ✅ | проблема, цель и связи читаются без устного контекста |
| 0.2 | Зафиксировать target/provider/inventory/sync design | ✅ | [`DESIGN.md`](./DESIGN.md) |
| 0.3 | Разбить реализацию на safety-ordered backlog | ✅ | этот файл |
| 0.4 | Добавить ссылки из rclone и sync-unification workstreams | ✅ | links resolve |

**Exit:** код не начат до фиксации identity, native-doc и state-isolation
решений.

## Phase 1 — Target foundation ✅

| ID | Task | Files | Acceptance |
|---|---|---|---|
| 1.1 | Добавить `TargetConfig` и `TargetContext` | new `tools/targets.py` | immutable resolved context содержит provider, inventory, sync, local/state paths |
| 1.2 | Parser `ydm_targets:v1` | `tools/targets.py` | unknown/missing keys дают path-specific error |
| 1.3 | Resolution: CLI → env → default → legacy | `ydm.py`, tools | все четыре приоритета покрыты тестами |
| 1.4 | Производные paths только из `state_dir` | target helpers | ни один state file не использует глобальный `var/` при активном target |
| 1.5 | `targets list|show|validate` | `ydm.py` или отдельный tool | offline, JSON + text, без обращения к remote |
| 1.6 | Example config без реальных данных | `ydm_targets.example.json` | synthetic paths/remotes only |
| 1.7 | Legacy regression suite | tests | без targets-файла CLI и defaults побайтно совместимы где возможно |

**Exit:** два synthetic targets не разделяют ни одного writable state path.

## Phase 2 — Storage schema v2 and identity ✅

| ID | Task | Files | Acceptance |
|---|---|---|---|
| 2.1 | Версионирование schema | `ydm.py`/new storage module | версия читается до scan; unsupported future schema не открывается на запись |
| 2.2 | Добавить scan source columns | storage migration | target/provider/inventory/remote сохранены |
| 2.3 | Добавить cloud identity/metadata columns | storage migration | object/parent IDs, MIME, hash type, native/shortcut flags |
| 2.4 | Заменить path unique index partial indexes | storage migration | sibling duplicate names сохраняются; duplicate object ID отвергается |
| 2.5 | Compatibility projection `md5` | storage/analyzer | старые MD5 reports не ломаются |
| 2.6 | Транзакционная миграция legacy DB | tests + migration | rollback при fault injection; повторный запуск idempotent |
| 2.7 | Checkpoint/restore с новой schema | `StorageManager` tests | tmpfs → disk → resume сохраняет все новые поля |

**Exit:** fake Google scan хранит два одинаковых path/name с разными IDs без
потери строк; legacy Yandex DB остаётся читаемой.

## Phase 3 — Provider-aware inventory

| ID | Task | Files | Acceptance |
|---|---|---|---|
| 3.1 | Разделить `CloudResourceClient` и sync backend | `ydm.py`, `tools/sync_backends.py` | выбор inventory не выбирает sync engine |
| 3.2 | `Capabilities` contract | new provider module | команды проверяют capability до execution |
| 3.3 | Нормализовать rclone `ID`, `MimeType`, metadata, hashes | `RcloneClient` | ни одно доступное identity поле не теряется |
| 3.4 | Определение native docs и shortcuts | Google provider adapter | fixtures всех типов классифицируются явно |
| 3.5 | Ошибка listing не превращается в пустую папку | scanner | failed folder keeps scan incomplete; no false deletions |
| 3.6 | `targets doctor` read-only probe | CLI | about/list/capabilities; никаких upload/delete/mkdir |
| 3.7 | Retry policy per folder | rclone inventory | transient failure retry bounded; resume сохраняется |

**Exit:** shallow Google scan завершается в отдельной DB и содержит IDs/MIME;
искусственный listing failure не выглядит успешным пустым деревом.

## Phase 4 — Read-only reports

| ID | Task | Files | Acceptance |
|---|---|---|---|
| 4.1 | `report status/scan-list/scan-info` target-aware | analyzer/CLI | target/provider видны в text и JSON |
| 4.2 | Diff classification strong/weak/uncomparable | Analyzer | hash, size+mtime и native metadata не смешиваются |
| 4.3 | Sibling-name collision report | Analyzer | обе cloud строки видны; status=`materialization_conflict` |
| 4.4 | Duplicate report по typed hash | Analyzer | сравниваются только одинаковые hash types; unhashable count отдельно |
| 4.5 | Long paths/tree для Google inventory | reports/tree | работают без local root |
| 4.6 | Unsupported provider reports | CLI | revision/trash-specific команда возвращает structured unsupported, не traceback |
| 4.7 | Read-only live validation | private runbook/results outside Git | временная DB, никакого apply |

**Exit:** Google Drive можно безопасно инвентаризировать и анализировать без
создания локальной копии.

## Phase 5 — Native documents and shortcuts

| ID | Task | Files | Acceptance |
|---|---|---|---|
| 5.1 | `native_docs=metadata|export|skip` validation | target config | default metadata; unknown mode refused |
| 5.2 | Metadata-only diff semantics | Analyzer | native doc не помечается missing local |
| 5.3 | Export plan schema | provider adapter | source ID, MIME, format и artifact path сохранены |
| 5.4 | Export только download-only | policy validator | bidirectional native/export rejected before filter write |
| 5.5 | Shortcut inventory semantics | scanner/tree | shortcut отдельный; target content не дублируется под shortcut path |
| 5.6 | Sanitized fixtures | tests | никаких captured live IDs/names |

**Exit:** native documents и shortcuts не могут молча попасть в обычный file
bisync.

## Phase 6 — Selective download-only

| ID | Task | Files | Acceptance |
|---|---|---|---|
| 6.1 | Policy v2 target binding | `sync_policy.py` | target/remote/root mismatch blocks apply |
| 6.2 | Target-scoped download filters | sync filters | filters written only under target state/local root |
| 6.3 | Collision/native preflight | policy validator | unmaterializable paths excluded with actionable reason |
| 6.4 | Download plan + dry-run | sync backend | exact source/destination/filter shown |
| 6.5 | Disposable filesystem-remote E2E | sync bench | selected paths copied, disabled absent, cleanup automatic |
| 6.6 | Disposable live Google folder | private acceptance | upload fixture separately, download, compare, remove fixture |
| 6.7 | Remove flow keeps local by default | menu/policy | local deletion requires explicit choice and successful check |

**Exit:** selected ordinary files and optional exports can be downloaded without
bidirectional state.

## Phase 7 — Bidirectional ordinary files

| ID | Task | Files | Acceptance |
|---|---|---|---|
| 7.1 | Eligibility gate | policy validator | native, shortcut, collision, missing access marker refused |
| 7.2 | Per-target bisync state/log/lock | `sync_bisync.py` | parallel targets cannot overwrite state; same target cannot overlap |
| 7.3 | Overlapping local-root detector | target validation | same/nested roots rejected across targets |
| 7.4 | Daemon conflict scoped by root | sync backend | active Yandex daemon blocks only intersecting rclone root |
| 7.5 | Filter hash/resync gate | bisync | changed scope cannot normal-run before resync |
| 7.6 | Delete/access safeguards | bisync | marker + max-delete + preview mandatory |
| 7.7 | Rename strategy capability-aware | sync rename | server-side move only when provider advertises it |
| 7.8 | Disposable live round-trip | private acceptance | create/edit/rename/delete fixture; no unrelated changes |

**Exit:** bidirectional mode доступен только для доказуемо материализируемых
обычных файлов в disposable-tested scope.

## Phase 8 — Menu, tree and scheduling

| ID | Task | Files | Acceptance |
|---|---|---|---|
| 8.1 | Target selector/header | `ydm_menu.py` | active target/provider всегда видимы |
| 8.2 | Immutable target through actions | menu modules | action не выполняет повторный auto-detect |
| 8.3 | Target-aware sync tree | `sync_tree.py` | policy/local/cloud читаются из одного context |
| 8.4 | Target-aware aliases | `tools/aliases.sh` | explicit target; no host paths in repo |
| 8.5 | Scheduler instance per target | termux/systemd helpers | separate job, lock, log and notification ID |
| 8.6 | Status separates reachability/inventory/sync | status/menu | четыре состояния не схлопнуты в `OK/error` |
| 8.7 | Cross-target bench | tests | Google action cannot mutate Yandex fixtures and наоборот |

**Exit:** обычная эксплуатация не требует вручную передавать DB/policy/filter
paths и не может незаметно выбрать другой target.

## Phase 9 — Provider-specific operations and publication gate

| ID | Task | Files | Acceptance |
|---|---|---|---|
| 9.1 | Capability-routed trash/cleanup | provider modules | unsupported operations explicit; plan→apply preserved |
| 9.2 | Google server-side move/rename | provider adapter | ID continuity checked in disposable fixture |
| 9.3 | Evaluate Google delta API | design addendum | measurement before implementation decision |
| 9.4 | Evaluate Shared Drives | design addendum | root/permissions/shortcuts documented |
| 9.5 | Secret/privacy scan of publishable commits | release checklist | diff + messages + filenames + fixtures inspected |
| 9.6 | Clean-checkout promotion rehearsal | private workflow | selected commits apply and tests pass without private overlay |
| 9.7 | Public docs migration | README/architecture/changelog | only after feature gates and sanitized evidence |

**Exit:** publishable core отделён от private deployment evidence; provider
extensions не размывают safety contracts.

## Risk register

| Risk | Mitigation |
|---|---|
| Одинаковые Google names схлопнутся в одну DB row | object ID identity + migration tests + mutation check |
| Native doc будет принят за пустой файл | `size < 0`/MIME classification; no generic bisync |
| Google target перезапишет Yandex state | state derived from target; cross-target sentinel tests |
| Auto-detect выберет daemon вместо Google rclone | explicit resolved TargetContext; no action-level detection |
| Ошибка listing станет массовым удалением | incomplete scan cannot become authoritative snapshot |
| Первый resync затронет реальное дерево | disposable cloud folder + separate local root + access marker |
| Live names/IDs попадут в Git | generated fixtures; ignored live state; publication gate |
| Generic abstraction скроет provider difference | capability errors, provider adapters, no fake common trash/revision API |
| Legacy Yandex установка сломается | no-target compatibility suite before Google write paths |

## Dependency graph

```text
Phase 1 targets
   └── Phase 2 identity/schema
          └── Phase 3 inventory
                 ├── Phase 4 reports
                 └── Phase 5 native docs
                        └── Phase 6 download-only
                               └── Phase 7 bidirectional
                                      └── Phase 8 UX/scheduler
                                             └── Phase 9 provider extras/publication
```

## Definition of done

- Existing Yandex tests and legacy CLI remain green.
- Two targets can run on one host with provably separate state and roots.
- Google inventory preserves object IDs and duplicate sibling names.
- Native documents cannot enter bidirectional sync.
- Read-only, download-only and bidirectional gates have independent acceptance.
- No live credentials, IDs, filenames, DB or logs exist in publishable commits.

