# Running YDM on Android

This is the setup the rclone backend, the policy layer and the scheduled
bisync were built on, written out in full. It is one working arrangement, not
the only one — but everything below was checked on the device on 2026-08-25,
and where something is expectation rather than measurement it says so.

Measured on: Android 15 (SDK 35), aarch64, Termux, proot-Debian 13.6,
Python 3.13.5, rclone v1.60.1-DEV.

## The one thing to understand first

**YDM does not run in Termux. It runs in a Debian container inside Termux, and
the automation runs in Termux and reaches into the container.**

The documentation used to say "Termux" throughout, which is the same class of
error as describing aliases that only existed in one person's shell: it names
an environment the project would not work in. Two halves, opposite directions:

```text
┌── Android ─────────────────────────────────────────────────────────────┐
│                                                                        │
│  ┌── Termux ─────────────────────────────────────────────────────────┐ │
│  │                                                                   │ │
│  │  termux-job-scheduler ── every 30 min ──> ~/ydm_bisync_job.sh     │ │
│  │                                             │                     │ │
│  │                                termux-wake-lock                   │ │
│  │                                             │                     │ │
│  │                                proot-distro login debian --bind…  │ │
│  │                                             │                     │ │
│  │  ┌── proot-Debian ───────────────────────────▼─────────────────┐  │ │
│  │  │                                                            │  │ │
│  │  │  tools/termux/job_run.sh                                   │  │ │
│  │  │    1. sync_rename.py preflight   (rename guard)            │  │ │
│  │  │    2. sync_bisync.py run --apply (rclone bisync)           │  │ │
│  │  │    3. ydm.py report prune        (once a day)              │  │ │
│  │  │                                                            │  │ │
│  │  │  the repository, python3, rclone, monitor.db               │  │ │
│  │  └────────────────────────────────────────────────────────────┘  │ │
│  │                                             │                     │ │
│  │                                termux-wake-unlock                 │ │
│  └───────────────────────────────────────────────────────────────────┘ │
│                                                                        │
│  /storage/emulated/0  (shared storage: the mirror, and the checkout)    │
└────────────────────────────────────────────────────────────────────────┘
```

The scheduler lives in Termux because that is where Android's JobScheduler is
reachable. The project lives in the container because that is where rclone and
a normal Python environment are. Everything awkward in this document comes
from that split.

## What goes where

| Thing | Path | Whose filesystem |
|---|---|---|
| Repository | `/root/notes/pro/ydm` | container, via a bind mount from shared storage |
| Local mirror | `/sdcard/Download/ya_disk` | shared storage, same path from both sides |
| `monitor.db` | `<repo>/monitor.db` | with the repository |
| Policy | `<repo>/var/sync_policy.json` | with the repository |
| Download filter | `/sdcard/Download/ya_disk.filters` | next to the mirror |
| Bidirectional filter | `/sdcard/Download/ya_disk.bisync.filters` | next to the mirror |
| rclone config | `~/.config/rclone/rclone.conf` | **container** |
| Scheduled job script | `~/ydm_bisync_job.sh` | **Termux**, not the repository |

The filters live next to the mirror because that is the convention every
rclone invocation already expects. The policy lives in `var/` because it is
YDM's own state, not a cloud object and not something a user should find in
their Downloads folder.

## Install

### 1. Termux and its API

Install Termux and Termux:API **from F-Droid** (the Play Store builds are
stale and cannot install each other's packages), then:

```bash
pkg install termux-api
termux-setup-storage        # grants /sdcard access; a permission dialog appears
```

`termux-api` the package is a client for the Termux:API *app*. Both are
needed, and `termux-job-scheduler`, `termux-wake-lock` and
`termux-notification` all come from it.

### 2. The container

```bash
pkg install proot-distro
proot-distro install debian
```

Give yourself a login alias with the binds you need — this is worth doing
before anything else, because forgetting the binds is the most common way to
lose an hour here:

```bash
alias d='proot-distro login debian \
  --bind /storage/emulated/0/Documents:/root/notes \
  --bind /storage/emulated/0/Download:/root/download'
```

**The trap.** `/root/notes` and `/root/download` are bind mounts, not part of
the container's rootfs. A bare `proot-distro login debian` has neither, so a
script that works in your interactive session fails from the scheduler, where
nobody passed `--bind`. The scheduled job therefore repeats the binds itself.

### 3. Inside the container

```bash
apt update && apt install -y python3 rclone git
git clone <this repo> /root/notes/pro/ydm
cd /root/notes/pro/ydm
python3 ydm.py init
```

Nothing here needs a Yandex OAuth token in `.env`: the rclone backend reads
the token from `rclone.conf`, and the tools that speak the API directly
(`cloud_delta.py`, `trash_scan.py`) accept `--token-source rclone`.

### 4. The remote and the mirror

```bash
rclone config          # n → name: yandex → storage: yandex → follow the prompts
mkdir -p /sdcard/Download/ya_disk
```

Then take a cloud snapshot and declare what you want synced. Nothing syncs
until the policy says so:

```bash
python3 ydm.py --backend rclone scan cloud --progress
python3 tools/sync_policy.py add --path /Books/Math --mode bidirectional \
  --local-root /sdcard/Download/ya_disk --apply
python3 tools/sync_policy.py render-filters \
  --local-root /sdcard/Download/ya_disk --apply
```

Modes are `bidirectional`, `download_only` and `disabled`. The distinction is
not decoration — see *Filenames* below for the case that forced it.

First bisync needs a baseline, and it is a dry run until you say otherwise:

```bash
python3 tools/sync_bisync.py resync --local-root /sdcard/Download/ya_disk \
  --filter-path /sdcard/Download/ya_disk.bisync.filters
# review the plan, then repeat with --apply
```

### 5. The scheduled job

From inside the container, with `/data/data/com.termux/files/home` reachable,
or from a Termux shell with the repository path in hand:

```bash
bash tools/termux/install_job.sh install
bash tools/termux/install_job.sh status
```

`install` copies `tools/termux/ydm_bisync_job.sh` into Termux's `$HOME` and
registers it. Both steps matter:

- **The copy must be in Termux's own `$HOME`.** Shared storage is a FUSE mount
  that silently ignores `chmod +x`. A job registered against a script on
  `/sdcard` is accepted and never runs.
- **The script cannot live in the repository.** `termux-job-scheduler`
  resolves paths in Termux's filesystem, and the repository only exists inside
  the container. The repository copy is a template; re-run `install` after
  editing it, or the two drift.

Defaults, all overridable by environment variable: job id `31415`, period
30 minutes (`YDM_JOB_PERIOD_MS`), persisted across reboots, skipped when
battery or storage is low. Android will not schedule anything more often than
every 15 minutes, and treats the period as a suggestion in any case.

Point the job at your own paths by exporting before `install`, or by editing
the copy:

```bash
export YDM_DIR=/root/notes/pro/ydm
export YDM_LOCAL_ROOT=/sdcard/Download/ya_disk
export YDM_BINDS="--bind /storage/emulated/0/Documents:/root/notes"
```

## What a run does

`tools/termux/job_run.sh`, inside the container:

1. **Rename preflight** (`sync_rename.py preflight`). A cloud-side rename must
   be settled before bisync sees a missing file and a new one and concludes
   delete-then-upload. The preflight makes its own local scan, which is why
   the database grows by one scan per run — and that scan is also the baseline
   the *next* run compares against.
2. **Bisync** (`sync_bisync.py run --apply`) with `--check-access`,
   `--max-delete 20` and a lock, using the **bidirectional** filter.
3. **Prune, once a day** (`ydm.py report prune --apply --vacuum`), gated by
   `var/prune_last`.

Run it by hand to see what it would do — without `--apply` the bisync stays a
dry run:

```bash
bash tools/termux/job_run.sh --local-root /sdcard/Download/ya_disk
```

Two behaviours worth knowing before you rely on the job:

**A failing preflight does not stop the sync.** That is deliberate — a broken
guard should not halt syncing — but it means the guard can be absent while the
log still says `run OK`. It happened for real: a schema change made the
preflight crash on every run, and nothing surfaced it. `job_run.sh` now writes
a `rename preflight FAILED` line to `var/bisync.log` when it happens. Check
for it before trusting a green log.

**Always pass the same `--local-root`.** The preflight identifies renames by
diffing its scan against the previous scan *of the same mirror*, so a hand-run
with a different root simply finds no baseline and skips a cycle rather than
comparing two unrelated trees. That skip is logged:

```
2026-08-28T04:10:02+00:00 rename preflight skip_bisync (no_comparable_baseline)
2026-08-28T04:40:11+00:00 rename preflight block_bisync (ambiguous_candidates)
2026-08-28T05:10:44+00:00 rename preflight skip_bisync (baseline_unreadable)
2026-08-28T05:40:09+00:00 rename preflight allow_bisync (error)
```

All four lines are new — until 2026-08-28 a guard decision produced no log entry
at all, so a stopped sync looked exactly like a job that never fired. Read them
as two pairs:

- `skip_bisync` — the guard had nothing to compare. `no_comparable_baseline`
  means no scan of *this* mirror was recorded, so check `--local-root`;
  `baseline_unreadable` means the database would not answer — see below for
  what actually does that. Both cost one cycle and clear on their own.
- `allow_bisync (error)` — bisync **ran without the guard**. The preflight
  reports its errors inside the JSON envelope and still exits 0, so this is not
  covered by the `FAILED rc=` line above. It is the one to chase: a run of these
  is issue #5 repeating.

**`baseline_unreadable` means VACUUM, not "someone was writing".** `monitor.db`
runs in WAL mode, where a writer does not block readers: a transaction held
across the whole preflight goes unnoticed (measured — 5.5 s of
`BEGIN EXCLUSIVE`, longer than the 5 s connect timeout, and the baseline still
read fine). What does block a reader is `VACUUM` or a checkpoint, and step 3 of
this same job is `report prune --apply --vacuum`. So the realistic cause is a
prune overlapping a preflight — a hand-run one, or two job cycles overlapping
because the vacuum took longer than the gap. Looking for a stray writer instead
is the wrong place, which is why this paragraph exists.

**All five lines were forced and confirmed on the device on 2026-08-29**
(issue #18), against this `job_run.sh`, with a scratch `YDM_VAR_DIR`, database
and mirror. Worth repeating after any change to the preflight, because none of
it is exercised by an ordinary run:

| Line | How to provoke it |
|---|---|
| `skip_bisync (no_comparable_baseline)` | a fresh database |
| `block_bisync (ambiguous_candidates)` | two files removed and two added at matching sizes, under a policy-covered directory |
| `allow_bisync (error)` | `--local-root` pointing at a directory that does not exist |
| `rename preflight FAILED rc=1` | a bad `schema` value in `var/rename_policy.json` |
| `skip_bisync (baseline_unreadable)` | another connection holding `PRAGMA locking_mode=EXCLUSIVE` |

Check `var/rename_policy.json` first: under `default_mode: observe` every
decision is `allow_bisync (observe_mode)` and no line is ever written, so an
empty log would prove nothing. The device runs `guard`.

An ordinary run — a baseline present, nothing renamed — writes no guard line at
all. That is the sixth case, and it is the one you should normally see.

**Prune is what keeps the database from growing without limit.** One local
scan per run is ~589 rows here; left alone that reached 3865 scans and 2.14
million rows — a 610 MB file of which `report prune` found 95.3% droppable.

## Filenames: what shared storage refuses

Android shared storage rejects filenames that Yandex Disk accepts, notably
`|` and `:`. Measured on the device, Android 15:

| Written to | Name with `\|` and `:` | Full-width `｜` and `：` |
|---|---|---|
| `/sdcard` | **refused** (`Operation not permitted`) | ok |
| container's own filesystem (`/tmp`) | ok | ok |

The control row is the useful half: the restriction is shared storage, not
proot, not Python, not the kernel.

Two ways to live with it:

- **Preventive, and preferred for a new mirror:** give the local side of the
  rclone remote an `encoding` that substitutes the characters before they
  reach the disk. `rclone copy` then succeeds, and `rclone lsf` gives the
  original name back:

  ```ini
  [android]
  type = local
  encoding = Slash,Dot,Colon,Pipe
  ```

  Verified as a mechanism on a desktop, and the device confirms the premise it
  rests on (the raw characters are what shared storage refuses). What has not
  been run end to end is a full `rclone copy` of an affected folder on the
  device itself.

- **Corrective, for a mirror that already exists:** `tools/repair_android_names.py`
  copies the incompatible files one by one under full-width lookalikes, after
  the normal sync has failed on them. It stays supported for exactly that
  case. It is deliberately *not* a broad encoded `rclone copy`: changing the
  encoding of an already-materialized mirror makes rclone treat existing files
  as different names and re-download them.

This is also why `download_only` exists as a policy mode. A path can be worth
mirroring locally and unsafe to sync back, because the local names are not the
cloud names. `/pro/agents` is the case that forced the distinction.

## The terminal

Confirmed on the device, and one correction to what this project used to say.

- **Finger swipe scrolls the output**, not the command history.
- **`ydm-help` fits** — no command or word breaks across lines. The help text
  is ASCII-only with short lines for exactly this reason.
- **Leaving a pager does not break scrolling.** `ydm-help` → `less` → `q` →
  swipe still scrolls.

If scrolling *is* broken, the symptom is not what you might expect from the
old wording ("swipes act like up/down arrows"). What you see is **touching the
screen printing junk characters into the command line**: the terminal has
mouse reporting enabled, encodes each tap as an escape sequence, and with no
application reading it, it lands in the input line. Clear it with:

```bash
printf '\033[?1000l\033[?1002l\033[?1003l\033[?1006l\033[?1015l'
tput rmcup; stty sane
```

On the maintainer's device that is wrapped in a `termux-scroll-fix` shell
function. It is **not** a Termux command and this project does not ship it —
if you want it, the three lines above are the whole thing.

A session can start in that state without a pager being involved; what put
this one there is not known, and it did not recur.

## Checking that it all works

```bash
bash tools/android_probe.sh                  # environment, filenames, tests, --help
python3 -m unittest discover -s tests -q     # 261 tests, 0 skips with rclone installed
python3 tools/sync_bisync.py status --local-root /sdcard/Download/ya_disk \
  --filter-path /sdcard/Download/ya_disk.bisync.filters
bash tools/termux/install_job.sh status      # is the job actually registered
tail -5 var/bisync.log                       # what the last runs did
```

`android_probe.sh` is the quickest honest answer to "does this work on my
phone": it prints which environment it is in, writes and deletes two probe
files with awkward names, runs the whole suite and checks three `--help`
commands. It needs no token and syncs nothing.

Note on the test count: GitHub Actions has no rclone, so eight rclone-backed
checks are skipped there. On a device with rclone installed they run — the
phone covers more than CI does, which is worth remembering when reading "CI is
green".

## Driving this from an agent inside the container

Optional, and not required for anything above. An agent working inside
proot-Debian can reach most of Termux directly — `/data/data/com.termux/files/usr/bin/termux-job-scheduler --pending`
works from the container, for instance. What it cannot do is run a command in
a *native* Termux session with Termux's own environment.

The maintainer's setup uses a small file-based job bridge for that: a
submit script writes a shell script into an inbox directory, a watcher running
in Termux executes it and writes stdout, stderr and an exit status back to an
outbox, and a wait script blocks until the status file appears. It is a
general Termux/proot tool with nothing YDM-specific in it, so it is documented
here rather than vendored. Nothing in this document needs it.

## Related

- [`tasks/rclone_backend/README.md`](../tasks/rclone_backend/README.md) — why
  rclone instead of the `yandex-disk` daemon on arm64, and the full toolset
- [`tasks/rclone_backend/POLICY_AWARE_BISYNC.md`](../tasks/rclone_backend/POLICY_AWARE_BISYNC.md)
  — the policy layer and what `download_only` protects against
- [`tasks/android_verify/`](../tasks/android_verify/README.md) — how the
  claims in this document were checked, and what stayed unverified
- [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) — the filename restriction as a
  reference entry
