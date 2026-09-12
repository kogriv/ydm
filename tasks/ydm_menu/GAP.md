# YDM Menu — Gap Analysis

*Дата:* 2026-08-08
*Статус:* зафиксировано после sync_tree v2 и запроса «диалоговый режим для человека»

## Контекст

Проект сознательно строился **agent-first**:

- JSON schema в каждом tool (`sync_policy`, `sync_tree:v2`, `sync_bisync`)
- dry-run по умолчанию, `--apply` явно
- bash-aliases как thin wrappers

На Android/Termux **основной пользователь — человек**, которому нужно:

1. Увидеть, что синкается
2. Добавить локальную папку (orphan `[L]`) в bisync
3. Убрать папку из sync безопасно
4. Понять, нужен ли resync / bisync run

Сейчас это требует знания 5+ команд и семантики меток.

---

## Симптомы (user-facing)

| Ситуация | Что делает человек сейчас | Боль |
|----------|---------------------------|------|
| Видит `[L] АнГем` в дереве | Не знает, что `[L]` = orphan | Нет подсказки «добавить в sync» |
| Хочет добавить АнГем в bisync | `ydm-sync-add /Books/Math/АнГем` | Кириллица, длинный путь, typos |
| После add | Может пропустить resync prompt | Bisync молча не подхватывает path |
| Хочет убрать папку | `ydm-sync-rm /path` | Страшно: delete-local без preview |
| Общая картина | `ydm-tree` + `ydm-sync-state` | Два экрана, нет единого места |
| Risk blocked | JSON / BLOCKED text | Непонятно: scan? download_only? abort? |
| Первый запуск | `ydm-help` (длинный) | Overwhelming для узкого экрана |

---

## Root causes

### G1. Нет слоя «intent → workflow»

Tools атомарны:

```text
sync_policy add     → policy file
sync_policy render  → filters
sync_bisync resync  → baseline
sync_bisync run     → sync
```

Человеку нужен **сценарий** «добавить локальную папку в bidirectional», а не
цепочка из 3–4 команд.

### G2. Orphan discovery не экспортирован как API

`sync_tree v2` **умеет** находить `[L]`, но только как text/JSON dump.
Нет `list_orphans()` для menu / pick-by-number.

### G3. Логика размазана между bash и Python

Критичные flows в `~/.bashrc`:

- `_ydm_offer_resync` — embedded Python heredoc
- `ydm-sync-add` — parse JSON + human messages
- `ydm-sync-pick` — rclone lsf + read

Дублирование, сложно тестировать, menu не может переиспользовать без copy-paste.

### G4. Add и remove asymmetry

| Операция | Текущий entry point | Обновляет policy? |
|----------|---------------------|-------------------|
| Add | `sync_policy.py add --apply` + render-filters | ✅ |
| Remove | `sync_filters.py remove --apply --delete-local` | ❌ (только filter-file legacy path) |

`ydm-sync-rm` **не вызывает** `sync_policy remove` — policy и filters могут
разойтись. Menu должен использовать **единый policy-first remove**.

### G5. Cloud pick vs local pick — разные UX

- `ydm-sync-pick` — только облако (`rclone lsf`), хорош для cloud-add
- Local orphan — только ручной path или чтение дерева глазами
- Нет «pick from orphans»

### G6. Resync / filter mismatch — скрытые состояния

`sync_bisync status` и `sync_policy status` оба expose `resync_needed`, но:

- разная семантика (legacy `.filters` vs bisync hash)
- человек видит это только если знает про `ydm-sync-state`
- после menu-action должно быть **явное** «следующий шаг»

### G7. Termux constraints

- Узкая ширина (~40–80 cols)
- Finger scroll vs shell history (см. README `termux-scroll-fix`)
- Cyrillic input unreliable → **выбор цифрами обязателен**
- Длинный JSON в stdout ломает UX

### G8. Нет «happy path» documentation для человека

README и `ydm-help` ориентированы на полный CLI reference.
Sync tree v2 docs — для агента/разработчика.
Нет одностраничного «как пользоваться руками».

### G9. «Add LOCAL folder» не находит локальную папку

*Добавлено 2026-09-12 по отчёту с устройства.*

Владелец скопировал `Books/Math/База2` на телефон (166 файлов, 1.6 ГБ) и
открыл пункт 3 — тот, который называется **Add LOCAL folder to sync**. Папки
в списке не было. Не из-за глубины, фильтра или политики: попасть туда она не
могла вообще.

`list_orphan_paths` строит список обходом **облачного снимка**
(`build_tree(analyzer, snapshot, …)`) и оставляет узлы с маркером `[L]`. То
есть `[L]` в текущей реализации читается как «в облаке есть, в политике нет,
локально лежит». У папки, созданной только локально, облачного узла нет —
значит нет и узла в дереве, значит она невидима. Наблюдаемое следствие: у всех
32 строк в списке `cloud files:` больше нуля, иначе они бы там не оказались.

То же и в дереве: `sync_tree --path /Books/Math --depth 1` показывает `База`
и не показывает `База2`. Так что это не дефект одного экрана, а **общая
слепота к тому, чего нет в облаке**, и экран лишь называется так, что обещает
обратное.

Чинить только меню нельзя. `test_the_orphan_list_agrees_with_the_tree`
закрепляет инвариант: меню и дерево выводят `[L]` разными путями и **обязаны
совпадать**, иначе меню предлагает добавить то, чего дерево не показывает.
Значит правка — в `build_tree`, и меню получает её следствием.

Ограничение, из которого вытекает решение: на устройстве каждый syscall
стоит ~0.2 мс (proot, ptrace), и Phase 12–13 занимались ровно тем, чтобы
дерево ничего не платило **за узел**. `os.listdir` на узел вернул бы эту
плату обратно — 5568 папок в свежем снимке. Значит один обход диска заранее,
в индекс, и константа на узел. Замерено: 97 папок до глубины 5 за 1.1 с.

### G10. Список сирот — плоская простыня без навигации

*Добавлено 2026-09-12, тот же отчёт.*

32 строки одним списком, выбор по номеру из всего списка, полные пути в каждой
строке. Из них 24 — `pro/mathcoach/*`, то есть один поддерев занимает три
четверти экрана. На узком экране телефона это не читается, а имена вида
`pro/agents/agents_week/Agents Week 2026 ｜ Лекция 1.1 Intro to AI Agents LLM`
переносятся и ломают нумерацию визуально.

Нужен спуск по уровням: показывать только непосредственных детей текущего
места, папки-контейнеры — со счётчиком сирот внутри, и «наверх». Пункт 2 (add
from cloud) уже устроен ближе к этому, так что расхождение ещё и внутреннее.

---

## Что уже хорошо (не ломать)

| Asset | Использование в menu |
|-------|---------------------|
| `sync_policy inspect` | risk report перед bidirectional |
| `sync_tree v2` | дерево + orphan markers |
| `sync_bisync status/run/resync` | status bar + actions |
| `ydm-sync-pick` logic | cloud folder picker (перенести в Python) |
| JSON tools | agent/automation без изменений |

---

## Acceptance: gap закрыт когда

1. `ydm` → add orphan → bidirectional **без ручного path** (цифры только).
2. Remove из menu обновляет **policy + filters** согласованно.
3. После add/remove menu **явно** предлагает resync/run если нужно.
4. Risk block показывает **человекочитаемый** выбор (scan / download / force / cancel).
5. Все agent commands (`ydm-sync-add`, …) работают как раньше.
6. Menu покрыто unit-тестами (prompt functions, orphan list, policy remove flow).
7. **(G9)** Папка, существующая только локально, видна и в дереве как `[L]`, и
   в пункте 3 — и обе стороны по-прежнему совпадают. Плата за узел не выросла.
8. **(G10)** Пункт 3 показывает один уровень за раз, а не весь список.

---

## Open questions (resolved in DESIGN)

1. Remove: policy-first или filters-first? → **policy-first** (DESIGN.md)
2. Один `ydm` или `ydm-menu`? → **оба alias**, primary `ydm`
3. Scan cloud из menu? → **да**, submenu «обновить облако» с pick path
