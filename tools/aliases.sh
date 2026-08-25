# YDM shell aliases. Source it, do not execute it:
#
#     echo 'source /path/to/ydm/tools/aliases.sh' >> ~/.bashrc
#
# Everything here is a thin wrapper that passes --db-path, --local-root and
# the filter/policy paths so the tools do not have to guess. Override any of
# them before sourcing:
#
#     export YDM_LOCAL_ROOT="$HOME/YandexDisk"
#     export YDM_DB="$HOME/.local/share/ydm/monitor.db"
#
# This file used to live only in one person's ~/.bashrc while the README
# described it, which meant the commands the README opened with did not exist
# after a clone. See tasks/opensource/GAP.md. The first version fixed that for
# scanning and the tree and stopped there — bisync, the rename guard and the
# policy layer had no wrappers at all, on the environment they were written
# for. This one carries the whole set.

# Where this file is, regardless of the caller's directory.
if [ -n "$BASH_SOURCE" ]; then
  YDM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
elif [ -n "$ZSH_VERSION" ]; then
  YDM_ROOT="$(cd "$(dirname "${(%):-%x}")/.." && pwd)"
fi
export YDM_ROOT
export YDM_DB="${YDM_DB:-$YDM_ROOT/monitor.db}"
export YDM_POLICY="${YDM_POLICY:-$YDM_ROOT/var/sync_policy.json}"
export YDM_REMOTE="${YDM_REMOTE:-yandex}"

# No default for the mirror: it is wherever you put it. Commands that need it
# say so instead of scanning a path that happens to exist.
export YDM_LOCAL_ROOT="${YDM_LOCAL_ROOT:-}"

_ydm_need_local_root() {
  if [ -z "$YDM_LOCAL_ROOT" ]; then
    echo "YDM_LOCAL_ROOT is not set — export it to your local mirror, e.g." >&2
    echo "  export YDM_LOCAL_ROOT=\"\$HOME/YandexDisk\"" >&2
    return 1
  fi
}

# The bidirectional filter, resolved late: YDM_LOCAL_ROOT may be exported
# after this file is sourced, and a value baked in at source time would be
# wrong for the rest of the session.
_ydm_bisync_filter() {
  if [ -n "$YDM_BISYNC_FILTER" ]; then
    printf '%s' "$YDM_BISYNC_FILTER"
  else
    printf '%s.bisync.filters' "${YDM_LOCAL_ROOT%/}"
  fi
}

# --- scanning ---------------------------------------------------------------

ydm-cli() {
  python3 "$YDM_ROOT/ydm.py" --db-path "$YDM_DB" "$@"
}

# A function rather than an alias: aliases are not expanded in non-interactive
# shells, so a script that sourced this file got nothing. It also takes the
# optional path the README has always documented.
ydm-scan-cloud() {
  if [ $# -gt 0 ]; then
    python3 "$YDM_ROOT/ydm.py" --db-path "$YDM_DB" scan cloud --progress --path "$1"
  else
    python3 "$YDM_ROOT/ydm.py" --db-path "$YDM_DB" scan cloud --progress
  fi
}

ydm-scan-cloud-path() {
  if [ -z "$1" ]; then
    echo "usage: ydm-scan-cloud-path <path>" >&2
    return 2
  fi
  python3 "$YDM_ROOT/ydm.py" --db-path "$YDM_DB" scan cloud --progress --path "$1"
}

ydm-scan-local() {
  _ydm_need_local_root || return 1
  python3 "$YDM_ROOT/ydm.py" --db-path "$YDM_DB" scan local --path "$YDM_LOCAL_ROOT"
}

# --- looking ----------------------------------------------------------------

# `ydm` is the interactive menu, which is what tasks/ydm_menu/HOW_TO_USE.md
# has documented since the menu existed. The raw CLI is `ydm-cli`.
ydm() {
  _ydm_need_local_root || return 1
  python3 "$YDM_ROOT/tools/ydm_menu.py" --db-path "$YDM_DB" \
    --local-root "$YDM_LOCAL_ROOT" --policy-path "$YDM_POLICY" \
    --bisync-filter-path "$(_ydm_bisync_filter)" "$@"
}

ydm-menu() {
  ydm "$@"
}

ydm-tree() {
  _ydm_need_local_root || return 1
  local path="/"
  local depth=4
  if [ $# -ge 1 ]; then
    if [[ "$1" =~ ^[0-9]+$ ]]; then
      depth="$1"
    else
      path="$1"
      if [ $# -ge 2 ] && [[ "$2" =~ ^[0-9]+$ ]]; then
        depth="$2"
      elif [ $# -ge 2 ]; then
        echo "usage: ydm-tree [depth]" >&2
        echo "       ydm-tree <path> [depth]" >&2
        return 2
      else
        depth=3
      fi
    fi
  fi
  python3 "$YDM_ROOT/tools/sync_tree.py" --db-path "$YDM_DB" \
    --local-root "$YDM_LOCAL_ROOT" --policy-path "$YDM_POLICY" \
    --path "$path" --depth "$depth" \
    --format text --text-tree --no-local-scan
}

ydm-tree-path() {
  _ydm_need_local_root || return 1
  python3 "$YDM_ROOT/tools/sync_tree.py" --db-path "$YDM_DB" \
    --local-root "$YDM_LOCAL_ROOT" --policy-path "$YDM_POLICY" \
    --path "$1" --depth "${2:-3}" \
    --format text --text-tree --show-all --no-local-scan
}

_ydm_bisync_status_json() {
  python3 "$YDM_ROOT/tools/sync_bisync.py" status --db-path "$YDM_DB" \
    --local-root "$YDM_LOCAL_ROOT" --filter-path "$(_ydm_bisync_filter)" \
    --format json
}

ydm-sync-state() {
  _ydm_need_local_root || return 1
  local status_json
  status_json="$(_ydm_bisync_status_json)" || return
  YDM_STATUS_JSON="$status_json" python3 - <<'PY'
import json
import os

payload = json.loads(os.environ["YDM_STATUS_JSON"])
data = payload.get("data") or {}
state = data.get("state") or {}
policy = data.get("policy") or {}
lock = data.get("lock") or {}
last_run = state.get("last_run_at") or "unknown"
last_status = state.get("last_status") or "unknown"
resync_needed = bool(data.get("resync_needed") or policy.get("policy_filter_resync_needed"))
locked = bool(lock.get("held"))

if locked:
    overall = f"BUSY (pid {lock.get('pid')})"
elif resync_needed:
    overall = "NEEDS RESYNC"
elif last_status == "ok":
    overall = "OK"
else:
    overall = f"CHECK ({last_status})"

print(f"YDM sync: {overall}")
print(f"Last run: {last_run} {last_status}")
print(f"Lock: {'yes' if locked else 'no'}")
print(f"Resync needed: {'yes' if resync_needed else 'no'}")
print()
print("Bidirectional:")
for item in policy.get("bidirectional_paths") or []:
    print(f"  {item}")
print("Download-only:")
for item in policy.get("download_only_paths") or []:
    print(f"  {item}")
print()
if locked:
    print("Next: wait for the current sync to finish")
elif resync_needed:
    print("Next: ydm-bisync-resync --apply")
else:
    print("Next: nothing")
PY
}

# --- changing what is synced ------------------------------------------------
#
# ydm-sync-add and ydm-sync-rm run with --apply: the change takes effect
# immediately, and on the yandex-disk daemon that means rewriting exclude-dirs
# and restarting it. There is no dry run here — use tools/sync_policy.py
# directly for that.

_ydm_offer_resync() {
  local status_json resync_needed answer
  status_json="$(_ydm_bisync_status_json)" || return
  resync_needed="$(YDM_STATUS_JSON="$status_json" python3 - <<'PY'
import json
import os

data = (json.loads(os.environ["YDM_STATUS_JSON"]).get("data") or {})
policy = data.get("policy") or {}
print("yes" if data.get("resync_needed") or policy.get("policy_filter_resync_needed") else "no")
PY
)"
  echo
  ydm-sync-state
  if [ "$resync_needed" = "yes" ]; then
    echo
    printf 'Run ydm-bisync-resync --apply now? [y/N] '
    read -r answer
    case "$answer" in
      y|Y|yes|YES) ydm-bisync-resync --apply ;;
      *) echo "Skipped. Scheduled bisync stays blocked until a resync is applied." ;;
    esac
  fi
}

ydm-sync-add() {
  _ydm_need_local_root || return 1
  local mode="bidirectional"
  local force_args=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --mode) mode="$2"; shift 2 ;;
      --force-risk) force_args+=(--force-risk); shift ;;
      -h|--help)
        echo "usage: ydm-sync-add [--mode bidirectional|download_only|disabled] [--force-risk] <path>"
        echo "default mode: bidirectional"
        return 0 ;;
      *) break ;;
    esac
  done
  local path="$1"
  if [ -z "$path" ]; then
    echo "usage: ydm-sync-add [--mode bidirectional|download_only|disabled] <path>" >&2
    return 2
  fi

  local add_json add_status
  add_json="$(python3 "$YDM_ROOT/tools/sync_policy.py" add --db-path "$YDM_DB" \
    --local-root "$YDM_LOCAL_ROOT" --policy-path "$YDM_POLICY" \
    --path "$path" --mode "$mode" "${force_args[@]}" --format json --apply)" || return
  add_status="$(YDM_ADD_JSON="$add_json" python3 - <<'PY'
import json
import os

payload = json.loads(os.environ["YDM_ADD_JSON"])
data = payload.get("data") or {}
risk = data.get("risk") or {}
error = data.get("error")
path = data.get("path") or risk.get("path") or "unknown"
mode = data.get("mode") or "unknown"
if error:
    print("BLOCKED")
    print(f"Path: {path}")
    print(f"Mode: {mode}")
    print(f"Reason: {error}")
    for item in risk.get("risks") or []:
        code = item.get("code", "risk")
        count = item.get("count")
        detail = f" ({count})" if count is not None else ""
        print(f"Risk: {code}{detail}")
    if any(item.get("code") == "path_not_found" for item in risk.get("risks") or []):
        print("Next: the path is in the cloud but not in the snapshot. Rescan, then retry:")
        print(f"  ydm-scan-cloud-path {path}")
    elif risk.get("recommended_mode"):
        print(f"Next: retry with --mode {risk['recommended_mode']}, or stop here.")
    raise SystemExit(1)
print("ADDED")
print(f"Path: {path}")
print(f"Mode: {mode}")
PY
)" || {
    printf '%s\n' "$add_status"
    return 1
  }
  printf '%s\n' "$add_status"

  python3 "$YDM_ROOT/tools/sync_policy.py" render-filters --db-path "$YDM_DB" \
    --local-root "$YDM_LOCAL_ROOT" --policy-path "$YDM_POLICY" \
    --format json --apply >/dev/null || return
  if [ "$mode" = "bidirectional" ]; then
    _ydm_offer_resync
  else
    ydm-sync-state
  fi
}

# Pick a child folder by number instead of typing a path — the folders here
# are commonly Cyrillic, and typing them on a phone keyboard is its own
# obstacle. Reads the cloud, so it needs the remote to be reachable.
ydm-sync-pick() {
  _ydm_need_local_root || return 1
  local parent="$1"
  if [ -z "$parent" ]; then
    echo "usage: ydm-sync-pick <cloud-parent-path>" >&2
    echo "example: ydm-sync-pick /Books/Math" >&2
    return 2
  fi
  local rows items=() line i choice picked full
  rows="$(rclone lsf "${YDM_REMOTE}:${parent#/}" --dirs-only --max-depth 1)" || return
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    items+=("${line%/}")
  done <<< "$rows"
  if [ "${#items[@]}" -eq 0 ]; then
    echo "No child folders under $parent" >&2
    return 1
  fi
  for i in "${!items[@]}"; do
    printf '%3d  %s/\n' "$((i + 1))" "${items[$i]}"
  done
  printf 'Choose number: '
  read -r choice
  if ! [[ "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -lt 1 ] || [ "$choice" -gt "${#items[@]}" ]; then
    echo "Invalid choice: $choice" >&2
    return 2
  fi
  picked="${items[$((choice - 1))]}"
  full="${parent%/}/$picked"
  echo "Selected: $full"
  ydm-sync-add "$full"
}

# Policy-first, and it offers to drop the local copy — which is why it goes
# through the menu's own action rather than editing the policy directly.
ydm-sync-rm() {
  _ydm_need_local_root || return 1
  local path="$1"
  if [ -z "$path" ]; then
    echo "usage: ydm-sync-rm <path>" >&2
    return 2
  fi
  YDM_RM_PATH="$path" YDM_RM_FILTER="$(_ydm_bisync_filter)" python3 - <<'PY'
import os
import sys

sys.path.insert(0, os.environ["YDM_ROOT"])
from tools.ydm_menu_actions import action_remove
from tools.ydm_menu_config import MenuConfig

cfg = MenuConfig.from_env_and_args(
    db_path=os.environ["YDM_DB"],
    local_root=os.environ["YDM_LOCAL_ROOT"],
    policy_path=os.environ["YDM_POLICY"],
    bisync_filter_path=os.environ["YDM_RM_FILTER"],
)
result = action_remove(cfg, os.environ["YDM_RM_PATH"], delete_local=True)
print(result.message)
sys.exit(0 if result.ok else 1)
PY
}

# --- bisync -----------------------------------------------------------------

ydm-bisync-run() {
  _ydm_need_local_root || return 1
  # The routine, safety-netted action — lock, --check-access, --max-delete cap
  # — and the same call the scheduled job makes.
  python3 "$YDM_ROOT/tools/sync_bisync.py" run --db-path "$YDM_DB" \
    --local-root "$YDM_LOCAL_ROOT" --filter-path "$(_ydm_bisync_filter)" \
    --format text --apply "$@"
}

ydm-bisync-resync() {
  _ydm_need_local_root || return 1
  # Dry run by default on purpose: a resync lets Path1 overwrite Path2. Pass
  # --apply yourself once the plan looks right.
  python3 "$YDM_ROOT/tools/sync_bisync.py" resync --db-path "$YDM_DB" \
    --local-root "$YDM_LOCAL_ROOT" --filter-path "$(_ydm_bisync_filter)" \
    --format text "$@"
}

ydm-bisync-status() {
  _ydm_need_local_root || return 1
  python3 "$YDM_ROOT/tools/sync_bisync.py" status --db-path "$YDM_DB" \
    --local-root "$YDM_LOCAL_ROOT" --filter-path "$(_ydm_bisync_filter)" \
    --format text "$@"
}

# --- renames ----------------------------------------------------------------
#
# A cloud-side rename has to be settled before bisync sees a missing file and
# a new one and concludes delete-then-upload. `ydm-rename` performs one
# explicitly; the rest drive the detector.

_ydm_rename() {
  local sub="$1"; shift
  python3 "$YDM_ROOT/tools/sync_rename.py" "$sub" --db-path "$YDM_DB" \
    --local-root "$YDM_LOCAL_ROOT" --bisync-filter-path "$(_ydm_bisync_filter)" \
    --format text "$@"
}

ydm-rename() {
  _ydm_need_local_root || return 1
  if [ $# -lt 2 ]; then
    echo "usage: ydm-rename <old-path> <new-path> [--apply]" >&2
    return 2
  fi
  local old="$1" new="$2"; shift 2
  _ydm_rename apply --old "$old" --new "$new" "$@"
}

ydm-rename-detect() { _ydm_need_local_root || return 1; _ydm_rename detect "$@"; }
ydm-rename-status() { _ydm_need_local_root || return 1; _ydm_rename status "$@"; }
ydm-rename-apply()  { _ydm_need_local_root || return 1; _ydm_rename apply-detected "$@"; }
ydm-rename-policy() { _ydm_need_local_root || return 1; _ydm_rename policy-status "$@"; }

ydm-rename-policy-set() {
  _ydm_need_local_root || return 1
  local mode="$1"
  if [ -z "$mode" ]; then
    echo "usage: ydm-rename-policy-set <mode> [path]" >&2
    return 2
  fi
  shift
  if [ $# -gt 0 ]; then
    _ydm_rename policy-set --mode "$mode" --path "$1"
  else
    _ydm_rename policy-set --mode "$mode"
  fi
}

# --- policy -----------------------------------------------------------------

_ydm_policy() {
  local sub="$1"; shift
  python3 "$YDM_ROOT/tools/sync_policy.py" "$sub" --db-path "$YDM_DB" \
    --local-root "$YDM_LOCAL_ROOT" --policy-path "$YDM_POLICY" \
    --format text "$@"
}

ydm-policy-status()  { _ydm_need_local_root || return 1; _ydm_policy status "$@"; }
ydm-policy-render()  { _ydm_need_local_root || return 1; _ydm_policy render-filters "$@"; }

ydm-policy-inspect() {
  _ydm_need_local_root || return 1
  if [ -z "$1" ]; then
    echo "usage: ydm-policy-inspect <path>" >&2
    return 2
  fi
  _ydm_policy inspect --path "$1"
}

# --- help -------------------------------------------------------------------

# Paged when there is a terminal to page into, because this is longer than a
# phone screen. `--plain` / `--no-pager` for pipes and for copying the lot.
_ydm_page() {
  if [ "${1:-}" = "--plain" ] || [ "${1:-}" = "--no-pager" ]; then
    cat
    return
  fi
  if [ -t 1 ] && command -v less >/dev/null 2>&1 && [ "${TERM:-dumb}" != "dumb" ]; then
    LESS="${LESS:--R -F -X}" less
  elif [ -t 1 ] && command -v more >/dev/null 2>&1 && [ "${TERM:-dumb}" != "dumb" ]; then
    more
  else
    cat
  fi
}

# ASCII-only and short-line on purpose: this is read on narrow terminals.
ydm-help() {
  # Unquoted heredoc: the Environment block shows the values in effect, which
  # is the first thing to check when a command misbehaves.
  cat <<EOF | _ydm_page "${1:-}"
YDM commands

Usage:
  ydm-help
  ydm-help --plain

Look:
  ydm                           Interactive menu (start here)
  ydm-menu                      Same as ydm
  ydm-tree [depth]              Sync tree from /, default depth 4
  ydm-tree <path> [depth]       Sync tree for one folder
  ydm-tree-path <path> [depth]  Same, with every branch shown
  ydm-sync-state                One-line sync status and the next action
  ydm-cli <command>             The raw CLI (ydm-cli report diff)

Scan:
  ydm-scan-cloud                Full cloud scan
  ydm-scan-cloud-path <path>    Cloud scan of one folder
  ydm-scan-local                Local scan of the mirror

Change what is synced (APPLIES IMMEDIATELY):
  ydm-sync-add <path>           Include a folder, bidirectional by default
  ydm-sync-add --mode <mode> <path>
                                bidirectional | download_only | disabled
  ydm-sync-pick <parent>        Choose a child folder by number
  ydm-sync-rm <path>            Exclude a folder, offers to drop the local copy

Bisync:
  ydm-bisync-status             State, lock, recent log lines
  ydm-bisync-run                One pass, applies
  ydm-bisync-resync             Baseline; DRY RUN until you pass --apply

Renames:
  ydm-rename <old> <new>        Rename one path safely
  ydm-rename-detect             Look for renames since the last scan
  ydm-rename-status             What the detector found
  ydm-rename-apply              Apply what it found
  ydm-rename-policy             Current rename policy
  ydm-rename-policy-set <mode> [path]

Policy:
  ydm-policy-status             Modes per path
  ydm-policy-inspect <path>     Why this path has this mode
  ydm-policy-render             Regenerate the rclone filters

Applies immediately, no dry run:
  ydm-sync-add, ydm-sync-rm, ydm-bisync-run, ydm-rename-apply
  For a dry run use the tool directly, e.g.
      python3 $YDM_ROOT/tools/sync_policy.py add --path <path> --mode <mode>

Environment:
  YDM_ROOT           $YDM_ROOT
  YDM_DB             $YDM_DB
  YDM_LOCAL_ROOT     ${YDM_LOCAL_ROOT:-(unset — export it before syncing)}
  YDM_POLICY         $YDM_POLICY
  YDM_BISYNC_FILTER  ${YDM_BISYNC_FILTER:-(derived from YDM_LOCAL_ROOT)}
  YDM_REMOTE         $YDM_REMOTE
EOF
}
