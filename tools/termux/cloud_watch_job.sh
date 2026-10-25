#!/data/data/com.termux/files/usr/bin/bash
# The Termux half of the cloud watcher: asks once whether Yandex Disk's
# resource API is answering again, and leaves the verdict to tools/cloud_probe.py.
#
# Same constraints as ydm_bisync_job.sh, for the same reasons — it must live in
# Termux's real $HOME (shared storage ignores chmod +x), it cannot live in the
# repository (the scheduler resolves paths in Termux's filesystem, and the
# repository only exists inside the container), and the binds must match what
# your login alias uses. Register it with `bash tools/termux/install_watch.sh
# install`; this file is the template and the installed one a copy.
#
# It never starts the sync. Output is thrown away on purpose — the answer is in
# var/cloud_watch.log, one line per run, including a line when the watcher
# itself broke. No new lines there means the job is not running.

set -u

DISTRO="${YDM_DISTRO:-debian}"
YDM_DIR="${YDM_DIR:-/root/notes/pro/ydm}"
YDM_BINDS="${YDM_BINDS:---bind /storage/emulated/0/Documents:/root/notes --bind /storage/emulated/0/Download:/root/download}"

termux-wake-lock

# shellcheck disable=SC2086  # YDM_BINDS is a deliberate word-split list.
proot-distro login "$DISTRO" $YDM_BINDS -- \
    python3 "$YDM_DIR/tools/cloud_probe.py" watch >/dev/null 2>&1

termux-wake-unlock
