# Multi-cloud targets: Yandex Disk + Google Drive

*Задача:* превратить YDM из приложения с одним неявным Яндекс.Диском в
профильно-изолированный монитор облачных дисков, сохранив весь существующий
Yandex flow и добавив Google Drive через уже настроенный rclone backend.

## Статус

- Design: ✅ [`DESIGN.md`](./DESIGN.md)
- Backlog: ✅ [`BACKLOG.md`](./BACKLOG.md)
- Implementation: ⬜ не начиналась
- Live Google Drive validation: ⬜ только после read-only и synthetic gates

## Почему это отдельный workstream

`RcloneClient` уже умеет перечислять произвольный remote, а аналитика YDM в
основном не зависит от API-провайдера. Но подстановка `gdrive:` вместо
`yandex:` не даёт безопасного Google Drive monitor:

- одна база, policy, filter-файлы и bisync-state сейчас не разделены по
  облачным целям;
- cloud row идентифицируется путём и именем, тогда как Google Drive допускает
  одинаковые имена в одной папке;
- rclone отдаёт стабильный Google object ID, MIME type и metadata, но текущая
  нормализация их выбрасывает;
- Google-native Docs/Sheets/Slides имеют особую export-семантику, размер `-1`
  и обычно не имеют content hash;
- часть команд относится к Яндекс API, а не к абстрактному облаку.

## Коротко о решении

Вводится сущность **target** — конкретная облачная цель со своим provider,
transport, remote, локальным корнем и полностью отдельным state directory.

```text
target yandex                     target gdrive
├── provider: yandex              ├── provider: google_drive
├── transport: daemon             ├── transport: rclone
├── own monitor.db                ├── own monitor.db
├── own policy + filters          ├── own policy + filters
└── own locks/logs/state           └── own locks/logs/state
```

Публичный интерфейс остаётся один:

```bash
python3 ydm.py --target yandex report status
python3 ydm.py --target gdrive scan cloud --progress
python3 tools/ydm_menu.py --target gdrive
```

Первый Google Drive milestone — read-only inventory и аналитика. Затем
добавляется выборочный `download_only`; bidirectional разрешается последним и
только для обычных файлов, которые можно корректно представить локально.

## Документы

- [`DESIGN.md`](./DESIGN.md) — target model, capability contract, schema v2,
  Google-native документы, безопасность и миграция.
- [`BACKLOG.md`](./BACKLOG.md) — фазы реализации с acceptance criteria.

## Связанные workstreams

- [`tasks/rclone_backend/`](../rclone_backend/) — существующий rclone transport
  и policy-aware bisync для Яндекс.Диска.
- [`tasks/sync_unification/`](../sync_unification/) — единый daemon/rclone CLI;
  этот workstream разделяет понятия provider, inventory и sync engine.
- [`tasks/sync_bench/`](../sync_bench/) — изолированный тестовый стенд без
  обращения к боевым данным.
- [`tasks/ydm_menu/`](../ydm_menu/) — интерфейс, который должен выбирать target,
  а не угадывать единственное облако по окружению.
