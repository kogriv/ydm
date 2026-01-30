# Рефакторинг: диагностика, архитектура и план

Папка содержит документы для **полноценного структурного и архитектурного рефакторинга** перед реализацией задач Smart Diff 2 и Sync Manager.

**Подход:** сначала рефакторинг кода (код в отдельном каталоге, разбиение на подпакеты, лёгкие паттерны), затем добавление фич (Smart Diff 2, Sync Manager). Без оверхеда, с учётом расширяемости.

---

## Документы

| Документ | Содержание |
|----------|------------|
| [DIAGNOSIS.md](DIAGNOSIS.md) | Диагностика текущего состояния кодовой базы (монолит ydm.py, exclude-dirs, дифф) и то, как она будет затронута реализацией Smart Diff 2 и Sync Manager. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Архитектура: расположение кода (src layout), слои (infrastructure → application → presentation), паттерны (передача зависимостей, чистые функции, единые точки входа команд) — без излишеств. |
| [REFACTORING_PLAN.md](REFACTORING_PLAN.md) | План рефакторинга: целевая структура (src/ydm/ с подпакетами config, storage, scan, analysis, sync, cli), назначение модулей, порядок по фазам, затем реализация фич. |

---

## Связь с задачами

- **tasks/smart_diff_2/** — задача «report diff --sync-only»; реализуется после рефакторинга (общая логика «path in sync» в ydm.sync.logic, вызов из ydm.analysis).
- **tasks/sync_manager/** — задача «sync tree / inspect / add»; реализуется после рефакторинга (дерево по снимку в ydm.analysis, статусы [S]/[P]/[-] в ydm.sync.logic, конфиг Yandex Disk в ydm.sync.config).
