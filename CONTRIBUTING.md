# Contributing to YDM

Thanks for considering a contribution. This is a small, stdlib-only Python
utility — the bar for contributing is intentionally low.

## Development setup

No build step, no virtualenv required (the project has zero external
dependencies by design):

```bash
git clone <repository-url>
cd ydm
python3 ydm.py --help
```

To exercise the cloud-facing commands you'll need either a Yandex Disk
OAuth token (`.env`, see `.env.example`) or an `rclone` remote configured
for `--backend rclone` (see [`tasks/rclone_backend/README.md`](tasks/rclone_backend/README.md)).
Purely local commands (`init`, `scan local`, `report *` against an
existing DB) need neither.

## CI

Every push/PR runs [`.github/workflows/ci.yml`](.github/workflows/ci.yml)
on Python 3.9/3.11/3.13: a syntax check (`py_compile`) of every `.py`
file, a `--help` smoke test of each CLI entry point, and
`tests/test_ydm_fixes.sh`. That's deliberately the minimum — it catches
syntax errors, broken `argparse` setup, and the regression suite, without
needing any credentials in CI. See below for what CI does *not* cover.

## Running the tests locally

There's no unit-test framework here — tests are bash scripts that drive
the real CLI:

- `bash tests/test_ydm_fixes.sh` — regression suite, pure local, no
  credentials or network needed. Runs in CI; run it locally too before
  opening a PR.
- `bash tests/test_rclone_backend.sh` — smoke test for `--backend rclone`.
  **Not run in CI** (needs a real, authorized `yandex:` remote in
  `rclone.conf`); it self-skips cleanly (`SKIP: ...`) if `rclone` isn't
  installed or no `yandex:` remote is configured, rather than failing.
- `bash tests/test_scan.sh` — integration test of cloud scanning on
  tmpfs. **Not run in CI** (hits the real Yandex Disk API via
  `--backend api`, needs `YANDEX_DISK_TOKEN`). Also has a known,
  pre-existing, out-of-scope bug: it monitors a hardcoded
  `/dev/shm/ydm_scan.db` path instead of the actual PID-namespaced
  `/dev/shm/ydm_scan_<pid>.db` — the scan itself still works, only the
  script's own progress-monitoring loop is watching the wrong file.

All three scripts create and clean up their own temporary database/files.

There's also [`tests/TEST_concurrent_protection.md`](tests/TEST_concurrent_protection.md)
— a manual QA runbook (not automated) for the lock-file/tmpfs concurrency
protection described in `docs/ARCHITECTURE.md` §12. Worth running by hand
if you're touching scan locking or checkpoint recovery.

## Code style

- Stdlib only — don't add a dependency without discussing it first in an
  issue.
- Match the existing conventions in the file you're editing: dry-run by
  default + explicit `--apply` for anything that mutates state, a JSON
  envelope (`{"success": bool, "data": {...}}`) for machine-readable
  output, never `sys.exit()` for expected error conditions (convey them
  via `success: false` instead).
- Keep commit messages descriptive and focused on the "why", not just the
  "what".

## Reporting bugs / proposing features

Open a GitHub issue. If it's a security issue, see
[SECURITY.md](SECURITY.md) instead of a public issue.
