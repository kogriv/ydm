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
TARGET_NAME="${YDM_TARGET:-legacy}"
TARGET="${YDM_JOB_PATH:-$HOME/ydm_bisync_job-${TARGET_NAME}.sh}"
PERIOD_MS="${YDM_JOB_PERIOD_MS:-1800000}"
# Stable per-target Android id. Explicit YDM_JOB_ID still wins.
TARGET_SUM=$(printf '%s' "$TARGET_NAME" | cksum | awk '{print $1}')
JOB_ID="${YDM_JOB_ID:-$((31415 + TARGET_SUM % 20000))}"
# One name for the binary, so the existence check and the calls cannot
# disagree, and so a test can point it at a recorder and assert on the
# arguments — the same reason job_run.sh names termux-notification through a
# variable. The absolute path is deliberate: Termux's bin is not on the
# container's PATH, and `remove` used to be reached from there.
JOB_BIN="${YDM_JOB_SCHEDULER_BIN:-/data/data/com.termux/files/usr/bin/termux-job-scheduler}"

need_termux() {
    if [ ! -x "$JOB_BIN" ]; then
        echo "termux-job-scheduler not found." >&2
        echo "  Install the Termux:API app *and* the package: pkg install termux-api" >&2
        echo "  Both are needed — the package alone is a client for the app." >&2
        exit 1
    fi
}

case "${1:-}" in
    install)
        need_termux
        if [ "$TARGET_NAME" != "legacy" ] && [ -z "${YDM_TARGETS_FILE:-}" ]; then
            echo "YDM_TARGETS_FILE is required when YDM_TARGET selects a target." >&2
            exit 2
        fi
        cp "$HERE/ydm_bisync_job.sh" "$TARGET"
        if [ "$TARGET_NAME" != "legacy" ]; then
            printf 'YDM_TARGET=%q\nYDM_TARGETS_FILE=%q\n' \
                "$TARGET_NAME" "$YDM_TARGETS_FILE" > "$TARGET.env"
            chmod 600 "$TARGET.env"
        fi
        chmod +x "$TARGET"
        if [ ! -x "$TARGET" ]; then
            echo "$TARGET is not executable after chmod +x." >&2
            echo "  That is what shared storage does. Pick a path in Termux's own \$HOME." >&2
            exit 1
        fi
        "$JOB_BIN" \
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
        "$JOB_BIN" --pending
        ;;
    remove)
        need_termux
        # `--cancel --job-id N`, not `--cancel N`. The job id is a flag of its
        # own and --cancel takes no value, so the positional form fails with
        # "termux-job-scheduler: too many arguments" and the job keeps running.
        # Found on 2026-10-12, with the sync down and this the documented way
        # to stop it: `--pending` still listed job 31415 afterwards, and the
        # script had said nothing to suggest otherwise.
        "$JOB_BIN" --cancel --job-id "$JOB_ID" || true

        # Then ask what is actually scheduled, rather than trusting the answer.
        # This tool's exit code does not carry the distinction: the broken form
        # above exits 1, but cancelling a job that was never there exits 0 with
        # "No job N found" — and only one of those two means the job is still
        # running. The end state is the only thing worth reporting, so it is
        # the thing that gets checked.
        if "$JOB_BIN" --pending 2>/dev/null | grep -q "Job $JOB_ID:"; then
            echo "Job $JOB_ID is STILL scheduled." >&2
            echo "  Cancel it by hand: $JOB_BIN --cancel --job-id $JOB_ID" >&2
            exit 1
        fi
        echo "Job $JOB_ID is not scheduled. The script stays at $TARGET."
        ;;
    *)
        sed -n '2,13p' "${BASH_SOURCE[0]}"
        exit 2
        ;;
esac
