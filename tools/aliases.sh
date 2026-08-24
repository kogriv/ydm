# YDM shell aliases. Source it, do not execute it:
#
#     echo 'source /path/to/ydm/tools/aliases.sh' >> ~/.bashrc
#
# Everything here is a thin wrapper that passes --db-path and --local-root so
# the tools do not have to guess. Override either before sourcing:
#
#     export YDM_LOCAL_ROOT="$HOME/YandexDisk"
#     export YDM_DB="$HOME/.local/share/ydm/monitor.db"
#
# This file used to live only in one person's ~/.bashrc while the README
# described it, which meant the commands the README opened with did not exist
# after a clone. See tasks/opensource/GAP.md.

# Where this file is, regardless of the caller's directory.
if [ -n "$BASH_SOURCE" ]; then
  YDM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
elif [ -n "$ZSH_VERSION" ]; then
  YDM_ROOT="$(cd "$(dirname "${(%):-%x}")/.." && pwd)"
fi
export YDM_ROOT
export YDM_DB="${YDM_DB:-$YDM_ROOT/monitor.db}"

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

# --- scanning ---------------------------------------------------------------

alias ydm="python3 \$YDM_ROOT/ydm.py --db-path \$YDM_DB"
alias ydm-scan-cloud="python3 \$YDM_ROOT/ydm.py --db-path \$YDM_DB scan cloud --progress"

ydm-scan-cloud-path() {
  python3 "$YDM_ROOT/ydm.py" --db-path "$YDM_DB" scan cloud --progress --path "$1"
}

ydm-scan-local() {
  _ydm_need_local_root || return 1
  python3 "$YDM_ROOT/ydm.py" --db-path "$YDM_DB" scan local --path "$YDM_LOCAL_ROOT"
}

# --- looking ----------------------------------------------------------------

ydm-menu() {
  _ydm_need_local_root || return 1
  python3 "$YDM_ROOT/tools/ydm_menu.py" --db-path "$YDM_DB" --local-root "$YDM_LOCAL_ROOT" "$@"
}

alias ydm-tree="python3 \$YDM_ROOT/tools/sync_tree.py --db-path \$YDM_DB --format text --text-tree"

ydm-tree-path() {
  local path="$1"
  local depth="${2:-2}"
  python3 "$YDM_ROOT/tools/sync_tree.py" --db-path "$YDM_DB" \
    --format text --text-tree --show-all --path "$path" --depth "$depth"
}

# --- changing what is synced ------------------------------------------------
#
# Both of these run with --apply: the change takes effect immediately, and on
# the yandex-disk daemon that means rewriting exclude-dirs and restarting it.
# There is no dry run here — use tools/sync_policy.py directly for that.

ydm-sync-add() {
  _ydm_need_local_root || return 1
  local mode="${2:-bidirectional}"
  python3 "$YDM_ROOT/tools/sync_policy.py" add \
    --db-path "$YDM_DB" \
    --local-root "$YDM_LOCAL_ROOT" \
    --mode "$mode" \
    --apply --path "$1"
}

ydm-sync-rm() {
  _ydm_need_local_root || return 1
  python3 "$YDM_ROOT/tools/sync_policy.py" add \
    --db-path "$YDM_DB" \
    --local-root "$YDM_LOCAL_ROOT" \
    --mode disabled \
    --apply --path "$1"
}

# --- help -------------------------------------------------------------------

ydm-help() {
  # Unquoted heredoc on purpose: the Environment block below shows the values
  # in effect, which is the first thing to check when a command misbehaves.
  cat <<EOF
YDM aliases:
  ydm                           Base command (use: ydm <command> --help)
  ydm-scan-cloud                Full cloud scan
  ydm-scan-cloud-path <path>    Cloud scan for one folder
  ydm-scan-local                Local scan of $YDM_LOCAL_ROOT
  ydm-menu                      Interactive sync UI (daemon or rclone)
  ydm-tree                      Sync tree with policy markers
  ydm-tree-path <path> [depth]  Full tree for one path, depth default 2
  ydm-sync-add <path> [mode]    Include folder in sync (default: bidirectional)
  ydm-sync-rm <path>            Exclude folder from sync

Modes for ydm-sync-add: bidirectional, download_only, disabled

  ydm-sync-add and ydm-sync-rm APPLY IMMEDIATELY. On the yandex-disk daemon
  that rewrites exclude-dirs and restarts it, which starts real syncing or
  real removal of local copies. For a dry run:
      python3 $YDM_ROOT/tools/sync_policy.py add --path <path> --mode <mode>

Environment:
  YDM_ROOT        $YDM_ROOT
  YDM_DB          $YDM_DB
  YDM_LOCAL_ROOT  ${YDM_LOCAL_ROOT:-(unset — export it before syncing)}

Quick start:
  ydm --help                    Full CLI help
  ydm scan --help               Scan commands
  ydm report --help             Report commands
EOF
}
