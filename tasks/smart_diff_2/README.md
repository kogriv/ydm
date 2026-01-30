# Smart Diff 2: Дифф в рамках синхронизируемого подмножества

## Краткое описание

Расширение отчёта о расхождениях: режим **sync-only** для `report diff` — показывать только пути, входящие в синхронизируемое подмножество (с учётом `exclude-dirs` конфига Yandex Disk).

## Задача

См. [TASK_SMART_DIFF_2.md](TASK_SMART_DIFF_2.md).

## Связи

- **smart_diff** (`tasks/smart_diff`) — источник снимка облака и логики диффа.
- **sync_manager** (`tasks/sync_manager`) — общая логика exclude-dirs и статусов sync (full/partial/excluded).
