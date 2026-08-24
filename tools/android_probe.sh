#!/bin/bash
# What YDM does not know about the Android side, asked as questions a machine
# can answer. Run it on the device, paste the output back.
#
#     bash tools/android_probe.sh
#
# Read-only with one exception: it creates probe files with deliberately awkward
# names under shared storage and deletes them again. It touches nothing that
# belongs to you, runs no sync, and needs no token.
#
# Background: tasks/android_verify/HANDOFF.md

set -u

pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; }
info() { echo "INFO  $*"; }
head2() { echo; echo "── $* ──"; }

echo "YDM Android probe — $(date -u '+%Y-%m-%d %H:%M UTC')"

# ---------------------------------------------------------------- environment

head2 "Environment"

info "uname: $(uname -a 2>/dev/null | cut -c1-100)"

# getprop lives in the Android system, which a proot container can still reach.
for gp in getprop /system/bin/getprop; do
    if command -v "$gp" >/dev/null 2>&1 || [ -x "$gp" ]; then
        info "android release: $("$gp" ro.build.version.release 2>/dev/null || echo '?')"
        info "android sdk:     $("$gp" ro.build.version.sdk 2>/dev/null || echo '?')"
        break
    fi
done

# Which of the two Android setups is this? The project's docs say "Termux";
# the machine it was developed on runs proot-Debian inside Termux, and the
# difference matters for paths and for which packages exist at all.
#
# Order matters: /etc/debian_version alone proves nothing — plain Debian and
# Ubuntu have it too. Establish Android first.
ON_ANDROID=no
for marker in /system/build.prop /system/bin/getprop /data/data/com.termux; do
    [ -e "$marker" ] && ON_ANDROID=yes && break
done

if [ "$ON_ANDROID" = no ]; then
    info "environment: not Android — this looks like an ordinary Linux host"
elif [ "${PREFIX:-}" = "/data/data/com.termux/files/usr" ]; then
    info "environment: Termux (native)"
elif [ -f /etc/debian_version ]; then
    info "environment: Android + Debian rootfs, i.e. proot ($(cat /etc/debian_version 2>/dev/null))"
else
    info "environment: Android, but neither Termux-native nor Debian — report what this is"
fi

info "python3: $(python3 --version 2>&1 | head -1)"
if command -v rclone >/dev/null 2>&1; then
    info "rclone:  $(rclone version 2>/dev/null | head -1)"
else
    info "rclone:  not installed (8 bench checks will skip)"
fi

# ------------------------------------------------------------------- storage

head2 "Shared storage"

STORAGE=""
for cand in /sdcard /storage/emulated/0 "$HOME/storage/shared" /mnt/sdcard; do
    if [ -d "$cand" ] && [ -w "$cand" ]; then STORAGE="$cand"; break; fi
done

if [ -z "$STORAGE" ]; then
    fail "no writable shared storage found — tried /sdcard, /storage/emulated/0, \$HOME/storage/shared, /mnt/sdcard"
    info "in Termux this usually means termux-setup-storage has not been run"
else
    info "using: $STORAGE"
fi

# --------------------------------------------------------- the filename question

head2 "Restricted characters (tasks/android_verify GAP G3)"

# The claim under test: shared storage rejects | and :, and rclone's encoding
# option sidesteps it by writing full-width look-alikes instead. If the first
# probe fails and the second succeeds, the encoding fix is the right answer and
# repair_android_names.py is working around something a config line solves.

probe_write() {
    # $1 = directory, $2 = filename, $3 = label
    local dir="$1" name="$2" label="$3" path
    path="$dir/$name"
    if : > "$path" 2>/dev/null; then
        rm -f "$path" 2>/dev/null
        echo "ok"
    else
        echo "refused"
    fi
}

RAW='ydm-probe-a|b:c.txt'
WIDE='ydm-probe-a｜b：c.txt'

if [ -n "$STORAGE" ]; then
    raw_result=$(probe_write "$STORAGE" "$RAW" raw)
    wide_result=$(probe_write "$STORAGE" "$WIDE" wide)

    info "shared storage, name with | and : → $raw_result"
    info "shared storage, full-width substitutes → $wide_result"

    if [ "$raw_result" = "refused" ] && [ "$wide_result" = "ok" ]; then
        pass "hypothesis confirmed: the characters are the problem, encoding is the cure"
    elif [ "$raw_result" = "ok" ]; then
        pass "no restriction here — this storage accepts | and : (report the paths above)"
    else
        fail "unexpected: raw=$raw_result wide=$wide_result — both need explaining"
    fi
fi

# The same two names on the container's own filesystem, as a control: if these
# also fail, the cause is not Android's shared storage at all.
ctrl_dir="${TMPDIR:-/tmp}"
info "control on $ctrl_dir: raw → $(probe_write "$ctrl_dir" "$RAW" ctrl-raw), wide → $(probe_write "$ctrl_dir" "$WIDE" ctrl-wide)"

# ----------------------------------------------------------------- test suite

head2 "Does YDM itself run here"

# The real question the project has never answered: it claims Android support,
# ships a tool for it and documents it, and nothing has ever checked that any
# of it starts on a device.

if [ -f ydm.py ] && [ -d tests ]; then
    if python3 -m unittest discover -s tests -q > /tmp/ydm_android_tests.log 2>&1; then
        pass "test suite: $(tail -3 /tmp/ydm_android_tests.log | grep -o 'Ran [0-9]* tests.*' || echo 'OK')"
        grep -o "skipped=[0-9]*" /tmp/ydm_android_tests.log | head -1 | while read -r s; do info "$s"; done
    else
        fail "test suite did not pass — last lines follow"
        tail -20 /tmp/ydm_android_tests.log
    fi
    info "full log: /tmp/ydm_android_tests.log"
else
    fail "run this from the repository root (ydm.py and tests/ must be here)"
fi

head2 "Command surface"

for cmd in "python3 ydm.py --help" "python3 tools/sync_tree.py --help" "python3 tools/ydm_menu.py --help"; do
    if $cmd >/dev/null 2>&1; then pass "$cmd"; else fail "$cmd"; fi
done

echo
echo "Done. Paste this output back, plus the manual notes from HANDOFF.md."
