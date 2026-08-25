#!/data/data/com.termux/files/usr/bin/bash
# Register, inspect or remove the scheduled sync job. Run this in Termux, not
# in the container: termux-job-scheduler is a Termux command and the job it
# registers is an Android JobScheduler entry.
#
#     bash tools/termux/install_job.sh install
#     bash tools/termux/install_job.sh status
#     bash tools/termux/install_job.sh remove
#
# `install` copies ydm_bisync_job.sh into Termux's $HOME first, because
# shared storage ignores chmod +x and a job registered against a file there
# never runs. Re-run it after editing the template.

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${YDM_JOB_PATH:-$HOME/ydm_bisync_job.sh}"
PERIOD_MS="${YDM_JOB_PERIOD_MS:-1800000}"
JOB_ID="${YDM_JOB_ID:-31415}"

need_termux() {
    if [ ! -x /data/data/com.termux/files/usr/bin/termux-job-scheduler ]; then
        echo "termux-job-scheduler not found." >&2
        echo "  Install the Termux:API app *and* the package: pkg install termux-api" >&2
        echo "  Both are needed — the package alone is a client for the app." >&2
        exit 1
    fi
}

case "${1:-}" in
    install)
        need_termux
        cp "$HERE/ydm_bisync_job.sh" "$TARGET"
        chmod +x "$TARGET"
        if [ ! -x "$TARGET" ]; then
            echo "$TARGET is not executable after chmod +x." >&2
            echo "  That is what shared storage does. Pick a path in Termux's own \$HOME." >&2
            exit 1
        fi
        termux-job-scheduler \
            --script "$TARGET" \
            --job-id "$JOB_ID" \
            --period-ms "$PERIOD_MS" \
            --persisted true \
            --battery-not-low true \
            --storage-not-low true
        echo "Registered job $JOB_ID -> $TARGET (every $((PERIOD_MS / 60000)) min)."
        echo "Android may run it less often than asked; that is the scheduler's call."
        ;;
    status)
        need_termux
        termux-job-scheduler --pending
        ;;
    remove)
        need_termux
        termux-job-scheduler --cancel "$JOB_ID"
        echo "Cancelled job $JOB_ID. The script stays at $TARGET."
        ;;
    *)
        sed -n '2,13p' "${BASH_SOURCE[0]}"
        exit 2
        ;;
esac
