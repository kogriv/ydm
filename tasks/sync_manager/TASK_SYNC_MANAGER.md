# Sync Manager: design notes and verified `yandex-disk` behavior

*Status:* the full `ydm.py sync {tree,inspect,add,remove,exclude-file}`
command design below was **never implemented as such** — `ydm.py` has no
`sync` subcommand. What exists instead is the transitional tools
(`tools/sync_tree.py`, `tools/sync_exclude.py`, and their rclone-backend
counterpart `tools/sync_filters.py`), which cover the `tree`/`add`/`remove`
parts of this design without the daemon-driven `inspect`/`exclude-file`
pieces. See [README.md](README.md) for what's actually current. It's not
clear the rest will get built as originally envisioned — treat the design
below as a reference for the underlying `yandex-disk` quirks it's built
around, not as a roadmap.

## Problem this was trying to solve

Managing `yandex-disk` sync by hand-editing
`~/.config/yandex-disk/config.cfg`'s `exclude-dirs` is error-prone:
- No visualization of what's actually syncing (the config only lists
  exclusions, not inclusions).
- Including a deeply-nested subfolder means excluding every sibling by
  hand.
- Some folder names (commas, square brackets) break `exclude-dirs`'
  comma-separated parsing.

## Verified `yandex-disk` behavior (still accurate, worth knowing)

These were tested directly against a real `yandex-disk` daemon
(06.01.2026) and are load-bearing facts for anyone touching sync tooling,
not just historical curiosities:

- **`exclude-dirs` does not work for files, only folders.** Tested with
  `video/m.jpg` (7.8MB) three ways: adding an already-local file to
  `exclude-dirs` doesn't delete it locally; deleting it locally while
  excluded correctly prevents re-download; but re-adding the file to the
  cloud while still excluded **does** sync it back locally anyway — the
  exclusion rule is silently ignored for files. The only way to keep a
  specific file out of sync is to move it into an excluded subfolder.
- **Bidirectional delete is real and applies everywhere.** Deleting a
  file locally (`rm`) deletes it in the cloud too — standard
  `yandex-disk` behavion on every platform, not a bug. `exclude-dirs`
  only affects folders one-way (stops them syncing); it doesn't give you
  "keep in cloud, remove locally" for individual files.
- **Renaming in the cloud via API** removes the old-named local copy
  automatically; the new name won't re-download if its folder is
  excluded.
- **Dangerous folder names** for `exclude-dirs`' comma-separated format:
  commas (`,`) split the list mid-name and corrupt it; square brackets
  (`[ ]`) are a lesser but real risk; names over ~200 characters risk
  filesystem issues. `tools/sync_exclude.py` warns on these.

## Design ideas not carried forward into an implementation

The original design additionally proposed: a `sync tree` command showing
the *entire* cloud tree with `[S]`/`[P]`/`[-]` sync-status markers at
every level (implemented, in `tools/sync_tree.py`); a `sync inspect
--path X` command listing a folder's children with danger-name warnings
(not implemented); `sync add`/`remove` with automatic sibling-exclusion
when including a nested subfolder (implemented, in
`tools/sync_exclude.py`); and a `sync exclude-file` command that moves a
file into an excluded subfolder via the API to work around the
files-aren't-excludable limitation above (not implemented).
