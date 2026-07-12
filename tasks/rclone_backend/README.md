# Rclone Backend — синхронизация и мониторинг Yandex Disk без демона yandex-disk

*Дата регистрации:* 10.01.2026
*Статус:* **Done** (Этапы 0, 1, 2, 4, 5, 6, 7 закрыты; Этап 3 покрыт частично
— реальная селективная локальная копия работает и проверена в рамках
Этапа 4, режимы "нет локали"/"импорт bisync .lst" не реализованы как
отдельные режимы, но полноценный `bisync` реализован в Этапе 7)
*Приоритет:* High (единственный рабочий путь для запуска ydm на arm64/Android)

> **Дисклеймер (для внешних читателей):** этот документ и весь код в
> `tools/sync_filters.py`/`sync_bisync.py`/связанные wrapper-скрипты
> написаны и проверены на одном конкретном устройстве — arm64
> proot-debian под Termux на Android, с конкретными путями
> (`/sdcard/Download/ya_disk`, `/data/data/com.termux/...`) и конкретным
> механизмом автоматизации (`termux-job-scheduler`). Это не
> универсальный кросс-платформенный рецепт — на другом Linux/macOS/Android
> устройстве пути, способ входа в окружение и механизм периодического
> запуска почти наверняка будут другими. Считайте это подробно
> задокументированным примером одного рабочего решения, а не готовым
> "из коробки" инструментом.

---

## 1. Проблема

Окружение **proot-debian на Android (arm64/aarch64)** не может использовать официальный
демон `yandex-disk`, на котором построена вся текущая архитектура ydm
(`scan local`, `report diff`, `tools/sync_exclude.py --apply` и т.д.):

- Официальный `yandex-disk` собирается Яндексом только под `amd64` и `i386`
  (проверено напрямую в `repo.yandex.ru/yandex-disk/deb/dists/stable/main/` — там
  только `binary-amd64` и `binary-i386`, репозиторий не обновлялся с апреля 2022).
- **Практически проверено на этом устройстве** (10.01.2026): пакет ставится через
  multiarch (`dpkg --add-architecture amd64` + `apt install yandex-disk:amd64`),
  но бинарник — `ELF 64-bit x86-64` — не запускается на aarch64:
  `Illegal instruction (SIGILL, exit 132)`. proot транслирует только syscalls,
  а не машинный код — эмуляция потребовала бы `qemu-user`/`box64`, что не годится
  для постоянно работающего демона синхронизации.
- Даже если бы демон завёлся, `/data/ya_disk` (путь из `notes/infra_hp/ya_disk/`) —
  это точка синхронизации на *другой* (amd64) машине, её тут нет.
- Диск на этом устройстве и так хронически в дефиците (см. историю переноса
  Telegram-папки и `ALL_RECOVERED_DOCUMENT` на gdrive) — тащить сюда полную
  локальную копию Яндекс.Диска, даже если бы это было технически возможно,
  контрпродуктивно.

При этом `rclone` в этом окружении уже работает и проверен (remote `gdrive`,
подробности в `notes/infra/proot_debian/rclone/`), а для Яндекс.Диска уже
задокументирован (но не настроен) подход через backend `yandex`
(`notes/infra/proot_debian/rclone/README.md`).

## 2. Ключевая идея

`rclone lsjson -R <remote:path>` отдаёт **одинаковую структуру записи**
(`Path`, `Name`, `Size`, `ModTime`, `IsDir`, `Hashes`) вне зависимости от
бэкенда — `yandex:`, `gdrive:` или обычный локальный путь. Значит, вся
аналитика ydm (`Analyzer`: `report diff`/`duplicates`/`long-paths`/
`analyze-scan`) не обязана знать, откуда взялись данные — переписывать нужно
**только слой сканирования/синхронизации**, а не ядро.

## 3. Предлагаемое решение

Не переписывать `ydm.py` с нуля и не городить отдельный инструмент рядом.
Ввести интерфейс `Backend` с двумя реализациями:

- `YandexApiBackend` — текущий код (прямые REST-вызовы). Остаётся для машин,
  где демон `yandex-disk` реально работает (amd64, `infra_hp`).
- `RcloneBackend` — новый, обёртка над `rclone` (subprocess). Используется
  в этом (arm64/Android) окружении.

Обе реализации кормят одинаковыми нормализованными записями существующий
`StorageManager`/SQLite — `Analyzer` и все `report`-команды работают
**без изменений** независимо от бэкенда. CLI и алиасы `ydm-*` не меняются.

### 3.1. Cloud scan (resumable, аналог CloudScanner)

Не гнать один `rclone lsjson -R` на весь диск (не резюмируется, при обрыве
на большом дереве — всё насмарку, что мы уже наблюдали с обрывами SOCKS-прокси
на скачивании HF-датасета). Вместо этого — тот же BFS/DFS по папкам с
чекпоинтами, что и сейчас в `CloudScanner`, только на каждом уровне вместо
вызова Yandex REST API — один невложенный вызов:

```bash
rclone lsjson yandex:path/to/folder --hash
```

Таблица `scan_progress` — та же; офсеты означают "какие папки уже обойдены",
а не "какая страница пагинации API". Graceful shutdown по SIGTERM/SIGINT —
как сейчас.

### 3.2. Локальная сторона — селективная, не полная копия

Правильная модель — не "скачать всё, чтобы сравнить", а тот же принцип
**выборочной синхронизации**, что был у `exclude-dirs`: локально лежит только
то, что явно включено в sync. Три источника для `report diff`:

1. **Нет** (по умолчанию) — cloud-only: дубликаты/длинные пути/junk работают
   чисто по облачному скану, без локальной стороны вообще.
2. **Реальная селективная копия** — папки, добавленные через `sync add`,
   физически лежат в `/root/notes/ya_disk/...` (материализуются `rclone copy`),
   остальное — нет. Тот же `LocalScanner` (`os.walk`), без изменений, просто
   сканирует не весь диск, а подмножество.
3. **Импорт снимка `rclone bisync`** — если где-то (например, на `infra_hp`)
   идёт полноценный bisync, его `~/.cache/rclone/bisync/*.lst` можно перенести
   сюда и сравнивать без сканирования локальных файлов вообще.

### 3.3. Sync management — замена `tools/sync_exclude.py --apply`

`exclude-dirs` в конфиге демона заменяется **filter-file** rclone
(`+ /Projects/2024/**` / `- **`, синтаксис уже описан в `notes/infra/proot_debian/rclone/README.md`).
Вместо "правим конфиг → демон сам разбирается" — два явных шага на команду:

```
ydm-sync-add /Projects/2024
  1. добавить "+ /Projects/2024/**" в filter-file
  2. rclone copy yandex:/Projects/2024 /root/notes/ya_disk/Projects/2024 --filter-from ... -P
     (реально материализует папку локально)

ydm-sync-rm /Projects/2024
  1. убрать правило из filter-file
  2. find /root/notes/ya_disk/Projects/2024 -mindepth 1 -delete (после подтверждения,
     та же схема "удалять только после rclone check", что уже отработана
     для Telegram-папки и ALL_RECOVERED_DOCUMENT)
```

`sync_tree.py` (визуализация `[S]/[P]/[-]`) почти не меняется — он и так
работает по снимку `monitor.db`, без обращения к облаку в моменте; просто
источник "что реально включено" теперь filter-file, а не `config.cfg`.

### 3.4. Junk cleanup (`tasks/junk/`)

`rclone delete <remote:path>` / `rclone deletefile` вместо прямых DELETE
к Yandex REST API. Двухфазная схема (`plan_cleanup.py` → `var/junk_list.txt`
→ подтверждение → `run_cleanup.py` → `var/deleted.log`) остаётся как есть —
она уже backend-агностична, меняется только примитив удаления.

### 3.5. Авторизация

`RcloneBackend` не использует `YANDEX_DISK_TOKEN`/`.env` вообще — нужен
`[yandex]` remote в `rclone.conf` (`rclone config`, тот же OAuth-флоу, что уже
проверен на `gdrive`). Токен из `.env` остаётся нужен только `YandexApiBackend`
(amd64-машина).

---

## 4. Технические детали

### 4.1. Формат `rclone lsjson`

```bash
rclone lsjson yandex:SomeFolder --hash
```
```json
[
  {
    "Path": "file.pdf",
    "Name": "file.pdf",
    "Size": 123456,
    "MimeType": "application/pdf",
    "ModTime": "2023-02-10T08:04:08.000Z",
    "IsDir": false,
    "Hashes": {"md5": "..."}
  }
]
```
Нужно на этапе 0 проверить: поддерживает ли rclone-бэкенд `yandex` отдачу
md5 (для дублей — сейчас `Analyzer` вероятно опирается на md5 из Yandex API;
надо свести схему полей 1:1 с тем, что ждёт `StorageManager`).

### 4.2. Filter-file вместо exclude-dirs

Синтаксис как в `notes/infra/proot_debian/rclone/README.md`:
```
+ /Projects/2024/**
+ /Docs/**
- **
```
`sync add`/`sync remove` — это программная генерация/правка такого файла
(не ручное редактирование), с тем же dry-run-by-default UX, что уже
реализован в `tools/sync_exclude.py`.

### 4.3. Масштаб (открытый вопрос)

Неизвестно, сколько файлов реально на аккаунте Yandex Disk (в отличие от
Google Drive/HF, размеры мы тут не проверяли). Это влияет на выбор между
subprocess-per-folder (`rclone lsjson` на каждую папку — просто, но
накладные расходы на процесс) и `rclone rcd` (remote control daemon —
меньше накладных расходов, но постоянный фоновый процесс). Смотреть на
Этапе 0.

---

## 5. Бэклог

### Этап 0 — Подготовка окружения

**Проверено (10.01.2026):** готовых креденшлов для Яндекс.Диска нет —
ни на VPS (`~/.secrets`, где лежит запечённый `gdrive`-токен), ни локально.
В отличие от `gdrive` (токен был запечён заранее), для `yandex:` нужна
**свежая OAuth-авторизация**. Окружение headless (нет браузера) →
авторизация выполняется **с телефона пользователя**: либо через
`rclone authorize "yandex"`, запущенный на устройстве с браузером (токен
передаётся сюда и вписывается в `rclone.conf` вручную), либо через
`rclone config` прямо в этой сессии со ссылкой, которую пользователь
открывает на телефоне. Способ — по месту, на момент выполнения этапа.

- [x] Авторизовать remote `yandex:` в `rclone.conf` — креды с телефона
      (OAuth через `rclone config create yandex yandex`, локальный колбэк
      `127.0.0.1:53682` открыт с телефона; refresh-токен валиден до 2027-07-10)
- [x] `rclone about yandex:` — проверить квоту/доступ →
      **2.011 TiB всего, 1.509 TiB занято, 514.081 GiB свободно**
- [x] `rclone lsjson yandex: --hash` на тестовой папке — сверить набор полей
      и наличие md5-хэшей с тем, что использует текущий `Analyzer` →
      **md5 отдаётся исправно** (`"Hashes":{"md5":"..."}` в каждой записи),
      совместимо с существующей дедуп-логикой без изменений
- [x] Прикинуть реальный масштаб (`rclone size yandex:`, количество файлов)
      → **74 756 файлов, 1.509 TiB** (полный `rclone size yandex: --json`,
      ~6 минут через прокси) → **решение: subprocess-per-folder**, не
      `rclone rcd`. При выборочных ~100-2000 файлов на папку верхнего уровня
      (сэмплы: `Job` — 945, `Docs` — 66) это низкие сотни вызовов
      `rclone lsjson` на полный обход дерева — overhead от spawn процессов
      под proot не критичен на таком масштабе, а `rclone rcd` (демон +
      HTTP RC) — преждевременная оптимизация, оправданная только на
      порядок больших объёмах (сотни тысяч+ файлов). Subprocess-per-folder
      к тому же проще резюмируется по чекпоинтам на уровне папок.

**Этап 0 закрыт (10.01.2026).**

### Этап 1 — Backend abstraction в `ydm.py`
- [x] Выделить интерфейс `Backend` — реализовано как `CloudResourceClient(ABC)`
      с методами `get_disk_info()` / `get_resources(path, limit, offset)`
      (уже, чем предполагалось в дизайне: `CloudScanner` в реальности зависит
      только от этих двух методов, отдельный `list_folder`/`delete` не нужны
      на этом этапе — `delete` появится в Этапе 5 вместе с junk cleanup)
- [x] Обернуть существующий REST-код — `YandexClient` теперь наследует
      `CloudResourceClient`, тело класса не тронуто →
      **поведение amd64-машины не изменилось** (проверено: `scan` без
      `--backend` при отсутствующем токене падает с тем же сообщением,
      что и раньше)
- [x] `--backend {api,rclone}` флаг (глобальный, дефолт `api`) +
      `RcloneClient(CloudResourceClient)` через `rclone lsjson --hash`/
      `rclone about --json`. Сквозной прогон подтверждён:
      `--backend rclone scan meta` (квота совпадает с `rclone about`),
      `--backend rclone scan cloud --path /tst` (2 файла, размер сошёлся
      с `rclone lsf`), `report scan-info`/`scan-list` отработали без
      единого изменения в `Analyzer` — данные из `RcloneClient` полностью
      совместимы по форме с данными из `YandexClient`.
      Побочный эффект (безопасный): `scan local` раньше требовал
      `YANDEX_DISK_TOKEN`, хотя вообще не пользовался клиентом — исправлено,
      токен/клиент теперь запрашиваются только для `target in (meta, cloud)`.

#### 1.1. Хардкоды, которые нужно вынести в конфиг/параметры

**Закрыто (10.01.2026).** Четыре из пяти мест сведены к одному источнику
дефолтов — `DEFAULT_CONFIG` в `ydm.py` (`local_root`, `exclude_config`),
импортируемому в `tools/*.py` вместо независимых литералов:

| Где | Было | Стало |
|---|---|---|
| `ydm.py` (`Analyzer.get_diff`, чтение `exclude-dirs` для `report diff`) | `os.path.expanduser("~/.config/yandex-disk/config.cfg")` | `os.path.expanduser(DEFAULT_CONFIG["exclude_config"])` |
| `ydm.py` (`scan local`, дефолт `--path`) | `local_path = ... else "/data/ya_disk"` | `... else self.args.config.get("local_root", "/data/ya_disk")` |
| `tools/sync_tree.py`, `tools/sync_exclude.py` (×2) | `--local-root` c литералом `"/data/ya_disk"` в трёх местах независимо | `default=DEFAULT_CONFIG["local_root"]`, импортировано из `ydm` |
| `tools/sync_common.py` (`load_exclude_dirs`) | свой независимый литерал `"~/.config/yandex-disk/config.cfg"` | `DEFAULT_CONFIG["exclude_config"]` — тот же источник, что и в `ydm.py` |

Пятое место — **`tools/sync_common.py:120-130`** (`run_command(["yandex-disk", ...])`,
`restart_daemon_systemctl`) — **сознательно не тронуто**: это не хардкод-литерал,
а целиком другая реализация (демон vs `rclone copy`-материализация). Параметризовать
здесь нечего — `RcloneBackend` не будет вызывать `yandex-disk` ни в каком виде.
Настоящая абстракция (`Backend.apply_sync_change()`, единая точка, за которой каждый
backend делает своё) — предмет **Этапа 4**, реализуется вместе с реальной
rclone-логикой синка, не раньше.

Проверено: `python3 -m py_compile` на всех изменённых файлах, `tools/sync_tree.py --help`
и `tools/sync_exclude.py add --help` показывают корректный дефолт `--local-root`,
импорт `tools.sync_common`/`tools.sync_tree`/`tools.sync_exclude` не сломан.

**Явно вне скоупа** (чтобы не раздувать этот заход): любой другой хардкод
в проекте, не связанный с выбором backend'а (например, пороги `checkpoint_*`
в `ydm_config.json` — они уже конфигурируемы, или пути внутри `tasks/junk/`,
`tasks/long_names/` — отдельные задачи, трогать по мере необходимости в
своих тасках).

### Этап 2 — `RcloneBackend`: cloud scan

**Закрыто (10.01.2026).** Фактически реализовано ещё в Этапе 1 (`RcloneClient`
подключился к `CloudScanner` без единой правки — тот и был весь объём работы),
здесь — полноценная проверка на реальном дереве, не на тестовой папке из 2 файлов.

- [x] Реализовать получение листинга папки через `rclone lsjson path --hash`
      → `RcloneClient._list_folder()` (Этап 1)
- [x] Встроить в существующий resumable `CloudScanner` — тот же `scan_progress`,
      тот же checkpoint/disk-flush механизм; подтверждено на прогоне ниже
      (RAM-БД в `/dev/shm/ydm_scan_<pid>.db` росла по ходу скана, `monitor.db`
      получал промежуточные чекпоинты)
- [x] `ydm.py --backend rclone scan cloud --path /pro --progress` — сквозной
      прогон на **реальном дереве** (`/pro`, не тестовая папка):
      **5565 файлов, 18 319 816 375 байт, 892 папки — оба числа побайтово
      совпали с `rclone size yandex:pro`**, 0 pending/in_progress по завершении,
      34 минуты через нестабильный SOCKS-прокси без единого падения процесса.
      `report scan-info`/`scan-list` показали то же самое без каких-либо
      изменений в `Analyzer`.
      *Ограничение:* сравнение с результатами `YandexApiBackend` на amd64-машине
      не проводилось — та машина недоступна из этой сессии. Структурная
      эквивалентность обеспечена конструктивно (оба клиента реализуют
      один `CloudResourceClient`, `CloudScanner`/`Analyzer` не знают о разнице),
      но проверить это можно, только когда пользователь запустит скан на
      amd64/`YandexApiBackend` и сравнит по тем же метрикам.
      *Resumability не проверена практически* (прокси не оборвался за 34
      минуты) — механизм не менялся ни строчкой относительно уже проверенного
      `YandexApiBackend`-пути, но реального обрыва-и-резюма на `RcloneClient`
      живьём не наблюдали.

### Этап 3 — `RcloneBackend`: локальная сторона
- [ ] Режим "нет локали" — `report diff` явно сообщает, что сравнение
      недоступно, остальные отчёты (`duplicates`/`long-paths`) работают
      (не проверялось: `report diff` не гоняли без локального скана вообще)
- [x] Режим "реальная селективная копия" — фактически реализован и проверен
      в рамках Этапа 4: `LocalScanner`/`run_local_scan` без единой правки
      сканирует `/sdcard/Download/ya_disk` (путь изменён с `/root/notes/ya_disk`
      по просьбе пользователя — портативность бинда, см. обновлённый
      `notes/infra/proot_debian/rclone/README.md`), куда `sync_filters.py add`
      материализует только явно включённые папки. `ydm-tree`/`sync_percent`
      подтвердили корректный подсчёт (DAO 100%, остальное 0%).
- [ ] Режим "импорт bisync .lst" — парсер формата `rclone bisync` listing
      в те же нормализованные записи (не требовался для текущего сценария —
      здесь используется `rclone copy`, не `bisync`)

### Этап 4 — Sync management

**Закрыто (10.01.2026).** Реализовано как отдельный CLI-инструмент
`tools/sync_filters.py` (по образцу `tools/sync_exclude.py`: dry-run по
умолчанию, JSON-схема `sync_filters:v1`, никаких интерактивных промптов —
только флаги), а не как подкоманда `ydm.py sync` — чтобы не переизобретать
диспетчер команд и не трогать работающий `--backend api` путь ради
симметрии. На этой машине `--backend api` не используется вообще —
`tools/sync_exclude.py` остаётся нетронутым для amd64/демон-машины.

- [x] `FilterFileManager` — реализовано как набор функций в
      `tools/sync_common.py` (`load_sync_filters`/`write_sync_filters`/
      `default_filter_path` + `rclone_copy_materialize`/`rclone_check_entry`/
      `delete_local_entry_contents`), аналогично `load_exclude_dirs` для
      `config.cfg`. `path_exists_in_snapshot` заодно вынесена из
      `sync_exclude.py` в `sync_common.py` — теперь используется обоими
      инструментами вместо дублирования.
- [x] `tools/sync_filters.py add --path X [--apply]` — dry-run по умолчанию
      (показывает план: что добавится в filter-file, что станет избыточным
      среди уже включённых потомков), `--apply` → правка файла + `rclone copy`
      материализация. **Проверено на реальном `yandex:`**: добавление `/tst`
      (2 файла) реально скачало их в `/sdcard/Download/ya_disk/tst`, `DAO` не
      тронута.
- [x] `tools/sync_filters.py remove --path X [--apply] [--delete-local]` —
      dry-run → план, `--apply` правит filter-file (без удаления), отдельный
      флаг `--delete-local` — только после `rclone check` с 0 расхождений
      реально чистит локальное содержимое (та же схема verify-then-delete,
      что для Telegram/`ALL_RECOVERED_DOCUMENT`, но через явный флаг вместо
      диалога, т.к. промпты запрещены дизайном). **Проверено**: `remove /tst
      --apply --delete-local` дал "0 differences found" → контент удалён,
      папка осталась пустой.
      Честно обработан краевой случай: попытка убрать подпапку внутри уже
      включённого предка (`DAO/1` при включённом `DAO`) даёт понятную ошибку
      вместо тихого не-действия — зеркально проблеме exclude-dirs с
      сиблингами (см. §9.2 `TASK_SYNC_MANAGER.md`), только в обратную сторону.
- [x] `tools/sync_tree.py` расширен флагом `--backend {api,rclone}` (дефолт
      `api` — поведение для amd64/демон-машины не изменилось). При
      `--backend rclone` — новая `compute_status_whitelist()`/
      `is_path_included()` (whitelist-логика: сам путь или предок явно
      включён → весь поддерева `[S]`, иначе `[P]`/`[-]` по потомкам) вместо
      blacklist-логики `compute_status()`. **Проверено на реальном дереве**:
      `--show-all` корректно показал 32 папки корня с единственной `[S] DAO`
      среди них; `--path /DAO --depth 3` показал `[S]` на всех уровнях
      поддерева; `sync_percent` посчитался верно (DAO 100%, корень 0%).
- [x] Алиасы в `~/.bashrc` — на этой машине их не было вообще (только в
      документации), заведены с нуля под rclone-режим: `ydm-scan-cloud`,
      `ydm-scan-cloud-path`, `ydm-scan-local`, `ydm-tree`, `ydm-tree-path`,
      `ydm-sync-add`, `ydm-sync-rm`, `ydm-help` — все с зашитыми
      `--backend rclone`/`--local-root /sdcard/Download/ya_disk`/абсолютным
      `--db-path`, так что работают из любой директории. Не git-tracked
      (`~/.bashrc` — не часть репозитория), машинно-специфичны по дизайну.

### Этап 5 — Junk cleanup на rclone

**Закрыто (10.01.2026).** `tasks/junk/run_cleanup.py` получил `--backend
{api,rclone}` (дефолт `api`, поведение не изменилось) и новый класс
`RcloneJunkClient` с точно тем же интерфейсом `.delete(path) -> status`,
что и `YandexClient` (`OK`/`404`/`429`/`ERR_*`) — вся остальная логика
`main()` (resume по `done_set`, ретраи на 429, построчный `var/deleted.log`)
не тронута ни строкой.

- [x] Ветка удаления через rclone: `rclone deletefile` для файлов,
      при ошибке ("is a directory or doesn't exist" — сообщение одинаковое
      что для папки, что для уже удалённого пути) — fallback на
      `rclone purge`, которая уже даёт различимый `404 - DiskNotFoundError`
      для реально отсутствующих путей. **Проверено вживую на всех трёх
      случаях** (файл/папка/уже-удалено) через прямые вызовы `RcloneJunkClient`
      на диспозабл-объектах в `yandex:tst/`.
- [x] **Корзина, а не безвозвратное удаление — проверено по-настоящему**,
      не только по документации: нашёлся флаг `--yandex-hard-delete`
      (default `false`, "Delete files permanently rather than putting them
      into the trash"), т.е. дефолт — как раз в корзину. Подтверждено
      прямым запросом к `GET /v1/disk/trash/resources` — удалённый через
      `rclone purge` тестовый объект оказался в `trash:/` с
      `origin_path: disk:/tst/...` и меткой времени удаления. Это то же
      поведение, что и `permanently=false` в оригинальном API-вызове.
- [x] `var/deleted.log` — формат не изменился (один путь на строку),
      **проверено сквозным прогоном** `run_cleanup.py --backend rclone` на
      одноразовом `var/junk_list.txt` (файл + папка): корректный прогресс-вывод,
      лог записался, повторный запуск верно показал "Nothing to do" (resume
      по логу работает). Тестовые артефакты вычищены из `var/` и с диска
      после проверки.

### Этап 6 — Документация и приёмка

**Закрыто (11.01.2026).**

- [x] Корневой `README.md` обновлён: раздел `Requirements`/`Installation`
      описывает оба способа доступа (`.env`+токен vs `rclone.conf`),
      `Global Flags` — новый `--backend`, добавлена секция "Управление
      синком без демона" с примерами `sync_filters.py`, файловая структура
      и список документации ссылаются на `tasks/rclone_backend/`.
- [x] `tests/test_ydm_fixes.sh` прогнан как регрессия (7/7 passed) —
      подтверждено, что рефакторинг Backend-интерфейса ничего не сломал
      в чисто локальной части (init/report на пустой БД, не трогает cloud).
      `tests/test_scan.sh` не адаптирован под `--backend` (он и так не
      принимает флагов вообще, плюс содержит не связанный с нами
      предсуществующий баг — мониторит несуществующий путь
      `/dev/shm/ydm_scan.db` вместо реального `ydm_scan_<pid>.db`; вне
      скоупа чинить). Вместо правки — новый `tests/test_rclone_backend.sh`
      (8 тестов: init, scan local без токена, `--backend rclone scan
      meta`/`scan cloud`, `report scan-list`/`duplicates`,
      `sync_filters.py list`, `sync_tree.py --backend rclone`) — **все 8
      прошли** на реальном `yandex:` remote, self-cleaning (временная БД и
      файлы удаляются в конце), скипается там, где `rclone`/remote нет.
- [x] Финальная сверка на реальных данных, не игрушечных: `report
      duplicates --scan-id 1` (полный аккаунт) нашёл реальные дубликаты
      (крупнейший — 9.7 GB, tar-архивы Spark, продублированные видео
      лекций), `report long-paths` — 299 путей длиннее 240 символов.
      `sync tree` показал корректное состояние (`DAO` и `video/Obsidian`
      — `[S]` 100%, остальное `[P]`/`[-]`). `plan_cleanup.py` нашёл 12
      реальных объектов мусора (`__pycache__` × 2, `.log` от LaTeX × 10);
      по решению пользователя добавлен новый флаг `--hard-delete` в
      `run_cleanup.py` (передаёт `--yandex-hard-delete` в rclone, минуя
      корзину) и **реально выполнено необратимое удаление** всех 12 —
      подтверждено запросом к `trash:/resources`, что удалённых объектов
      там нет (в отличие от предыдущих тестовых soft-delete в Этапе 5).
      По ходу приёмки заодно найден и исправлен мелкий
      предсуществующий баг: `plan_cleanup.py` падал, если `var/` не
      существует (не создавал директорию перед записью) — добавлен
      `os.makedirs(..., exist_ok=True)`.

### Этап 7 — Двусторонняя синхронизация (`rclone bisync`)

**Закрыто (12.07.2026).**

- [x] Новые файлы: `tools/sync_bisync.py` (подкоманды `resync`/`run`/
      `status`, dry-run по умолчанию, JSON/text рендер — те же конвенции,
      что у `sync_filters.py`), плюс новые хелперы в `tools/sync_common.py`
      (`rclone_bisync_run`, `acquire_lock`/`release_lock`,
      `load_bisync_state`/`save_bisync_state`, `filter_file_hash`,
      `append_text_log`, `notify`). Триггер — Termux-side
      `ydm_bisync_job.sh` (`termux-wake-lock` → `proot-distro login
      debian -- ...` → `termux-wake-unlock`), зарегистрирован через
      `termux-job-scheduler` (период 30 мин).
- [x] Решение по триггеру: периодический опрос через
      `termux-job-scheduler` (Android JobScheduler), а не inotify-хук —
      на этом окружении нет `systemd`/`cron`, а постоянно живой
      `inotifywait`-процесс (нужен для хука) требовал бы
      `termux-wake-lock` на постоянной основе — то есть вышел бы
      *дороже* по батарее, чем редкий периодический таймер, а не
      дешевле, как казалось интуитивно на старте обсуждения.
- [x] Fallback'и: `--check-access` + сентинел `RCLONE_TEST` (защита от
      сценария «`/sdcard` не примонтирован в эту сессию → локально пусто
      → bisync предлагает удалить всё в облаке»); явный `--max-delete 20`
      (в установленной сборке rclone v1.60.1-DEV это *глобальный* флаг с
      default `-1`/безлимит, а не bisync-специфичный `--max-delete 50`,
      как в актуальной документации rclone — обнаружено при исследовании
      флагов перед реализацией); PID-lock (`/tmp/ydm_bisync.lock`, тот же
      паттерн, что у `ydm.py`'s cloud-scan lock, `ydm.py` ~2635-2670);
      проверка sha256-хэша filter-файла перед `run` — если фильтры
      менялись после последнего `resync`, `run` отказывается запускать
      rclone и требует явного `resync --apply` (вместо того, чтобы
      напороться на непрозрачную внутреннюю блокировку самого `bisync`).
- [x] `resync --apply` выполнен реально (12.07.2026): база установлена по
      3 папкам (`DAO`, `pro/mathcoach`, `video/Obsidian`), 365 файлов
      сверено, удалена 1 пустая директория (`pro/mathcoach/pedo`) в
      пределах `--max-delete`.
- [x] Round-trip тест выполнен реально: тестовый файл создан локально →
      `run --apply` → подтверждено появление в облаке (`rclone lsjson`);
      файл удалён локально → `run --apply` → подтверждено исчезновение из
      облака (`rclone lsjson` — файла больше нет). Обратное направление
      (создание/удаление в облаке → локально) отдельно не тестировалось,
      но следует из той же механики `bisync`.
- [x] **Находка:** удаление через `--backend rclone bisync` занимает
      **~6 минут даже для одного маленького файла**. Причина — не в
      `sync_bisync.py` и не в rclone-клиенте как таковом, а в самом
      Yandex Disk REST API: удаление (перенос в корзину) — асинхронная
      операция (HTTP 202 + ссылка на статус операции). Бэкенд rclone
      (`backend/yandex/yandex.go`, функция `waitForJob()`) опрашивает эту
      ссылку раз в секунду **без единой строки лога** между попытками —
      подтверждено на `-vv` (максимальная вербозность): тишина в течение
      всей паузы. Подтверждено дважды независимо на одном и том же по
      характеру маленьком текстовом файле: `run --apply` через
      `sync_bisync.py` (6m37s) и отдельный ручной `rclone bisync -vv` в
      обход инструмента (6m25.7s). Листинг папок (с фильтрацией) в обоих
      прогонах занял ~1 секунду — задержка не связана с объёмом аккаунта
      (74 756 файлов, 1.509 TiB), а именно с латентностью асинхронной
      операции удаления на стороне Yandex. Есть флаг
      `--yandex-hard-delete` (permanent delete, минуя корзину) — из
      документации rclone неясно, ускоряет ли он это, но он **сознательно
      не включён по умолчанию** в `sync_bisync.py`: убирает Trash как
      fallback от случайного/массового удаления — одну из ключевых
      страховок дизайна Этапа 7. Практическое следствие:
      `termux-wake-lock` в wrapper-скрипте — не опциональная
      подстраховка, а обязательное условие (без него Android с высокой
      вероятностью убьёт процесс до завершения асинхронного удаления).
      30-минутный период джоба даёт запас (6 мин << 30 мин) для
      одиночных удалений; поведение при нескольких одновременных
      удалениях в одном прогоне не проверялось (см. §8).
- [x] **Найден и исправлен баг в wrapper-скрипте при реальной проверке
      пользователем.** Первая версия `ydm_bisync_job.sh` вызывала голый
      `proot-distro login debian -- bash -c "cd /root/notes/pro/ydm && ..."`
      — без `--bind`. `/root/notes` внутри debian-proot **не часть
      rootfs**, а bind-mount `/storage/emulated/0/Documents` (алиас `d` в
      `~/.bashrc`: `proot-distro login debian --bind
      /storage/emulated/0/Documents:/root/notes --bind
      /storage/emulated/0/Download:/root/download`); без явного `--bind`
      путь просто не существует. Это не поймалось при автономном
      тестировании, потому что все мои проверки шли уже изнутри
      правильно смонтированной debian-сессии — тест «голого» входа
      изнутри той же сессии сам оказался вложенным (`proot-distro login`
      из-под уже активного proot) и падал на другой, не относящейся к
      делу ошибке (`execve(/usr/bin/env): No such file or directory`).
      Реальная причина нашлась только когда пользователь запустил
      `bash ~/ydm_bisync_job.sh` из настоящего, свежего Termux-шелла и
      получил honest `cd: /root/notes/pro/ydm: No such file or
      directory`. Исправлено: wrapper теперь передаёт оба `--bind` explicitly,
      как в алиасе `d`.
- [x] **Вторая, независимая находка при повторной проверке пользователем.**
      После фикса `--bind` скрипт доходил до `sync_bisync.py`, но каждый
      прогон логировался как `run BLOCKED ... Filter-file changed since
      the last resync` — при том что содержимое `ya_disk.filters`
      фактически не менялось (хэш совпадал с сохранённым в
      `bisync_state.json`). Причина: wrapper вызывал `sync_bisync.py run
      --apply` **без `--local-root`**, поэтому подставлялся стухший
      default из `ydm.py` (`DEFAULT_CONFIG["local_root"] =
      "/data/ya_disk"`, путь с другой, amd64-машины) вместо реального
      `/sdcard/Download/ya_disk`. `filter-path` резолвился в
      несуществующий `/data/ya_disk.filters` →
      `filter_file_hash()` возвращал `None` → не совпадало с сохранённым
      хэшем → guard срабатывал (по правильной логике, но по неверной
      причине). Страховка отработала как задумано — просто повод для неё
      был другой, чем предполагался. Исправлено: wrapper теперь явно
      передаёт `--local-root /sdcard/Download/ya_disk`.

---

## 6. Примеры использования (целевой UX, после реализации)

```bash
# Разовая настройка
rclone config   # remote "yandex"

# Облачный скан через rclone-бэкенд
python3 ydm.py --backend rclone scan cloud --progress

# Отчёты работают как обычно
python3 ydm.py report duplicates --scan-id 1
python3 ydm.py report long-paths --scan-id 1

# Управление синком (материализует/убирает локальную копию)
python3 ydm.py sync add --path /Projects/2024
python3 ydm.py sync add --path /Projects/2024 --apply
python3 ydm.py sync remove --path /Projects/2024 --apply
```

---

## 7. Связанные задачи

- `tasks/sync_manager/` — оригинальный дизайн управления синхронизацией
  (демон-based); этот таск переиспользует его модель (`tree`/`inspect`/
  `add`/`remove`, dry-run by default, JSON-схемы) под другой backend.
- `tasks/smart_diff_2/` — `report diff --sync-only`; должен работать
  поверх `RcloneBackend` так же, как поверх `YandexApiBackend`.
- `tasks/junk/` — источник паттерна двухфазной (`plan` → `apply`) очистки,
  переиспользуемого в Этапе 5.
- `notes/infra/proot_debian/rclone/README.md` — существующий, уже рабочий
  рецепт rclone+Yandex Disk (filter-файлы, `copy`/`bisync`) в этом
  окружении — основа для `RcloneBackend`.

## 8. Открытые вопросы / риски

- ~~Поддерживает ли rclone-бэкенд `yandex` серверные хэши (md5) через
  `lsjson --hash`~~ — **закрыто на Этапе 0:** да, md5 отдаётся в каждой
  записи.
- ~~Реальный масштаб аккаунта~~ — **закрыто на Этапе 0:** 74 756 файлов,
  1.509 TiB → решение зафиксировано: subprocess-per-folder.
- SOCKS-прокси (Xray) уже показал себя нестабильным под нагрузкой
  (обрывы `ConnectTimeout`/`SSL handshake timeout` при скачивании HF-датасета
  с 8 и даже 2 воркерами) — резюмируемый посёлочный (per-folder) скан
  устойчив к единичным обрывам по конструкции, но стоит заложить retry-обёртку
  по аналогии с `download_habr_dataset.sh`.
- Нужно решить: `ydm-sync-add`/`ydm-sync-rm` алиасы в `~/.bashrc` —
  общие для всех машин (с автоопределением backend'а) или дублировать
  под rclone-версию отдельно.
- (Этап 7) Не проверено, как масштабируется ~6-минутная асинхронная
  пауза Yandex Disk на удалении (см. Этап 7), если в одном прогоне
  `bisync` удаляется сразу много файлов — идут ли `waitForJob()`-опросы
  параллельно (по числу `--transfers`) или последовательно по одному.
  При последовательном поведении и достаточно большом батче удалений
  прогон теоретически может не уложиться в паузу до следующего
  срабатывания `termux-job-scheduler` (сейчас 30 мин) — лок-файл не даст
  повреждения данных, просто следующий тик будет пропущен/сдвинут.
