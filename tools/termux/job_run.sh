#!/usr/bin/env bash
# The container half of the scheduled Android sync job.
#
# This runs *inside* proot-Debian, where the repository and rclone live. The
# Termux half (tools/termux/ydm_bisync_job.sh) only takes a wake lock and
# enters the container; everything with an opinion is here, so it can be run
# and tested by hand:
#
#     bash tools/termux/job_run.sh --local-root /sdcard/Download/ya_disk
#     bash tools/termux/job_run.sh --local-root /sdcard/Download/ya_disk --apply
#
# Without --apply nothing is written: the preflight still runs (it is
# read-only apart from its own local scan) and the bisync stays a dry run.
#
# Order matters. The rename preflight runs first because a cloud-side rename
# that cannot be represented on /sdcard must be settled before bisync sees a
# missing file and a new one, and decides that means delete-and-upload.
set -u

PROJECT_DIR="${YDM_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
LOCAL_ROOT="${YDM_LOCAL_ROOT:-}"
FILTER_PATH="${YDM_BISYNC_FILTER:-}"
DB_PATH="${YDM_DB:-}"
PRUNE_INTERVAL_SEC="${YDM_PRUNE_INTERVAL_SEC:-86400}"
APPLY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --local-root) LOCAL_ROOT="$2"; shift 2 ;;
        --filter-path) FILTER_PATH="$2"; shift 2 ;;
        --db-path) DB_PATH="$2"; shift 2 ;;
        --project-dir) PROJECT_DIR="$2"; shift 2 ;;
        --prune-interval) PRUNE_INTERVAL_SEC="$2"; shift 2 ;;
        --apply) APPLY=1; shift ;;
        --no-apply) APPLY=0; shift ;;
        -h|--help)
            sed -n '2,20p' "${BASH_SOURCE[0]}"
            exit 0 ;;
        *) echo "job_run.sh: unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [ -z "$LOCAL_ROOT" ]; then
    echo "job_run.sh: --local-root is required (or set YDM_LOCAL_ROOT)." >&2
    echo "  There is no default: guessing means syncing the wrong folder." >&2
    exit 2
fi

cd "$PROJECT_DIR" || exit 2

# The bidirectional filter, not the download one. Some paths are worth
# mirroring locally and unsafe to sync back — see
# tasks/rclone_backend/POLICY_AWARE_BISYNC.md.
[ -n "$FILTER_PATH" ] || FILTER_PATH="${LOCAL_ROOT%/}.bisync.filters"
[ -n "$DB_PATH" ] || DB_PATH="$PROJECT_DIR/monitor.db"

# The absolute path is deliberate — Termux's bin is not on the container's PATH
# — but it made the notification the one thing here a test could not reach or
# stub, so `tests/test_job_run.py` sent a real one to the phone every time it
# exercised the block branch. Invisible on CI, where the binary does not exist;
# on the device it looked exactly like the guard stopping a live sync. Named
# through a variable so a test can point it at a recorder and assert on it.
NOTIFY_BIN="${YDM_NOTIFY_BIN:-/data/data/com.termux/files/usr/bin/termux-notification}"
NOTIFY_REMOVE_BIN="${YDM_NOTIFY_REMOVE_BIN:-/data/data/com.termux/files/usr/bin/termux-notification-remove}"
# Its own id, separate from sync_bisync's: the two say different things and one
# must not silently replace the other. Both are ids rather than nothing, so a
# card can be taken back once what it describes stops being true — see
# tools/sync_common.py, SYNC_NOTIFICATION_ID.
NOTIFY_ID="ydm-guard"

notify() {
    [ -x "$NOTIFY_BIN" ] || return 0
    "$NOTIFY_BIN" --title "$1" --content "$2" --id "$NOTIFY_ID" --alert-once \
        >/dev/null 2>&1 || true
}

dismiss_notify() {
    [ -x "$NOTIFY_REMOVE_BIN" ] || return 0
    "$NOTIFY_REMOVE_BIN" "$NOTIFY_ID" >/dev/null 2>&1 || true
}

# The same file sync_bisync.py appends to. It resolves this through
# var_path(), which YDM_VAR_DIR redirects, so writing a bare `var/` here would
# split the log in two the moment anything sets it.
BISYNC_LOG="${YDM_VAR_DIR:-$PROJECT_DIR/var}/bisync.log"

# Every run that does not do the ordinary thing says so in the log. Only
# `sync_bisync.py` used to write there, so a guard decision left no trace at
# all: the log showed `run OK` lines, then a gap, then `run OK` again, and
# nothing distinguished "the guard stopped it" from "the job never fired". The
# notification is not a substitute — it is not kept.
log_decision() {
    printf '%s rename preflight %s (%s)\n' "$(date -Is)" "$1" "$2" \
        >> "$BISYNC_LOG" 2>/dev/null || true
}

# --- 1. rename preflight ----------------------------------------------------

preflight_json=$(python3 tools/sync_rename.py preflight \
    --db-path "$DB_PATH" \
    --local-root "$LOCAL_ROOT" \
    --bisync-filter-path "$FILTER_PATH" \
    --format json 2>/dev/null)
preflight_rc=$?

preflight_field() {
    printf "%s" "$preflight_json" | PF_KEY="$1" PF_FALLBACK="$2" python3 -c '
import json, os, sys
try:
    print(json.load(sys.stdin).get("data", {}).get(os.environ["PF_KEY"])
          or os.environ["PF_FALLBACK"])
except Exception:
    print(os.environ["PF_FALLBACK"])
'
}

decision=$(preflight_field decision allow_bisync)
preflight_reason() { preflight_field reason unknown; }

# A preflight that cannot answer must not stop syncing — but say so, because
# this is the guard being absent, not the guard passing. On 2026-08-24 a
# schema change made the preflight crash on every run for hours, and the only
# visible sign was `run OK` in var/bisync.log. See issue #5.
if [ "$preflight_rc" -ne 0 ]; then
    decision="allow_bisync"
    echo "job_run.sh: rename preflight failed (rc=$preflight_rc); running without the guard." >&2
    printf '%s rename preflight FAILED rc=%s\n' "$(date -Is)" "$preflight_rc" \
        >> "$BISYNC_LOG" 2>/dev/null || true
elif [ "$decision" = "allow_bisync" ] && [ "$(preflight_reason)" = "error" ]; then
    # The guard passing and the guard being absent both end up allowing bisync,
    # and only one of them is fine. An error inside the preflight travels in the
    # JSON envelope with exit code 0, so the rc test above does not see it, and
    # without this line a run with no guard at all looks exactly like a clean
    # one. That is issue #5, which cost hours of `run OK`.
    log_decision allow_bisync error
fi

# --- 2. bisync --------------------------------------------------------------

case "$decision" in
    skip_bisync)
        log_decision skip_bisync "$(preflight_reason)"
        ;;
    block_bisync)
        log_decision block_bisync "$(preflight_reason)"
        notify "ydm rename preflight" "bisync blocked; run sync_rename.py status"
        ;;
    *)
        # The guard let this run through, so whatever it complained about last
        # time is over. The card goes with it.
        dismiss_notify
        if [ "$APPLY" -eq 1 ]; then
            python3 tools/sync_bisync.py run --apply \
                --db-path "$DB_PATH" \
                --local-root "$LOCAL_ROOT" \
                --filter-path "$FILTER_PATH" >/dev/null 2>&1
        else
            python3 tools/sync_bisync.py run \
                --db-path "$DB_PATH" \
                --local-root "$LOCAL_ROOT" \
                --filter-path "$FILTER_PATH"
        fi
        ;;
esac

# --- 3. housekeeping, once a day -------------------------------------------
#
# Every run of this job adds a local scan to the database (the preflight makes
# one), and nothing used to remove them: 3865 scans and 2.14 million rows had
# accumulated by 2026-08-24 — 610 MB, of which `report prune` found 95.3%
# droppable. The stamp is written only on success, so a failed prune retries
# on the next run instead of being skipped for a day, and a failure here never
# stops syncing.

[ "$APPLY" -eq 1 ] || exit 0

stamp=var/prune_last
now=$(date +%s)
last=0
if [ -f "$stamp" ]; then
    last=$(cat "$stamp" 2>/dev/null)
fi
case "$last" in
    ""|*[!0-9]*) last=0 ;;
esac

if [ $(( now - last )) -ge "$PRUNE_INTERVAL_SEC" ]; then
    {
        printf "=== %s ===\n" "$(date -Is)"
        python3 ydm.py --db-path "$DB_PATH" report prune --apply --vacuum
    } >> var/prune.log 2>&1 && printf "%s" "$now" > "$stamp"
fi

exit 0
