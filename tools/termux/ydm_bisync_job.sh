#!/data/data/com.termux/files/usr/bin/bash
# The Termux half of the scheduled Android sync job.
#
# Install it by copying this file into Termux's own $HOME and registering it
# with termux-job-scheduler — see docs/ANDROID_SETUP.md. Two details are not
# preferences:
#
#   * It must live in Termux's real $HOME, not on /sdcard. Shared storage is a
#     FUSE mount that silently ignores chmod +x, so a copy there is registered
#     happily and never runs.
#
#   * It cannot live in the repository, because the repository only exists
#     inside the proot container while termux-job-scheduler resolves script
#     paths in Termux's own filesystem. This copy is the template; the
#     installed one is a copy, and they drift unless you re-copy.
#
# termux-wake-lock / -unlock bracket the run because a scheduled job otherwise
# gets a short execution window and Android suspends the CPU mid-transfer.
#
# YDM_BINDS is required if your checkout or your mirror lives on shared
# storage. /root/notes and /root/download inside the container are bind
# mounts, not part of the rootfs: a bare `proot-distro login debian` has no
# /root/notes at all, and the job then fails on a directory that exists in
# every interactive session. Match this to the binds your login alias uses.

set -u

DISTRO="${YDM_DISTRO:-debian}"
YDM_DIR="${YDM_DIR:-/root/notes/pro/ydm}"
YDM_LOCAL_ROOT="${YDM_LOCAL_ROOT:-/sdcard/Download/ya_disk}"
YDM_BINDS="${YDM_BINDS:---bind /storage/emulated/0/Documents:/root/notes --bind /storage/emulated/0/Download:/root/download}"

termux-wake-lock

# shellcheck disable=SC2086  # YDM_BINDS is a deliberate word-split list.
proot-distro login "$DISTRO" $YDM_BINDS -- \
    bash "$YDM_DIR/tools/termux/job_run.sh" \
    --project-dir "$YDM_DIR" \
    --local-root "$YDM_LOCAL_ROOT" \
    --apply >/dev/null 2>&1

termux-wake-unlock
