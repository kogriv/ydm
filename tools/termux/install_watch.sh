#!/data/data/com.termux/files/usr/bin/bash
# Register, inspect or remove the cloud watcher. Run this in Termux, like
# install_job.sh, which does the work — this only gives the watcher its own
# job id, script and period so the two never share a registration:
#
#     bash tools/termux/install_watch.sh install
#     bash tools/termux/install_watch.sh status
#     bash tools/termux/install_watch.sh remove
#
# Job 31413 every 4 hours, script ~/ydm_cloud_watch.sh. `remove` checks
# `--pending` afterwards, as install_job.sh's does.

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The watcher is about the Yandex Disk API, not about a target: a leftover
# YDM_TARGET from the shell must not turn this into a named-target install.
unset YDM_TARGET YDM_TARGETS_FILE

export YDM_JOB_TEMPLATE="cloud_watch_job.sh"
export YDM_JOB_ID="${YDM_WATCH_JOB_ID:-31413}"
export YDM_JOB_PATH="${YDM_WATCH_JOB_PATH:-$HOME/ydm_cloud_watch.sh}"
export YDM_JOB_PERIOD_MS="${YDM_WATCH_PERIOD_MS:-14400000}"

exec bash "$HERE/install_job.sh" "$@"
