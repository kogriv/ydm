# Long filenames: AI-assisted renaming (proof-of-concept, never run in production)

**Status: unfinished experiment.** `smart_renamer.py` was written, code
reviewed, and patched to v1.1 — but there's no evidence it was ever
actually run against the real account (no `var/rename_plan_*`/
`rename_success_*` output files anywhere). Kept here as a working
proof-of-concept in case anyone wants to pick it up, not as a documented,
supported feature.

## Problem

The `yandex-disk` daemon fails with a generic "access error" on files
whose *name* (not full path) exceeds ext4's 255-byte limit — real cloud
accounts can accumulate filenames up to ~350+ bytes (long titles,
Cyrillic text inflates UTF-8 byte length, files migrated from Windows/NTFS
which has looser limits). Those files exist in the cloud (readable via
the API, so `monitor.db` sees them) but can never sync locally.

## Approach

`smart_renamer.py`: query `monitor.db` for filenames over a byte
threshold, send each through a **local** LLM via [Ollama](https://ollama.com)
(no cloud API, no added runtime dependency for the rest of the project —
this script is the one place in the repo that assumes an optional
external service) asking for a short, transliterated, meaning-preserving
name, then rename via the Yandex Disk API (cloud-side — the files aren't
local, so there's nothing to rename on disk). Handles rename-collision
suffixing, skips `*_files/` web-archive folders, and backs off on API
rate limits (429).

## Known limitations

Never tested end-to-end against real data. The design also doesn't
address one edge case explicitly: if the AI-generated name is *itself*
still too long or empty, the script has a length-validation guard but no
guarantee of a sensible fallback name.

## If you want to pick this up

The script and its Ollama dependency are self-contained in this folder —
nothing else in the project touches it. See
[CONTRIBUTING.md](../../CONTRIBUTING.md).
