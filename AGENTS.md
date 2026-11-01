# YDM for agents

YDM audits a Yandex Disk account and keeps chosen folders in sync with a local
mirror through `rclone bisync`, or through the `yandex-disk` daemon where that
still works. These commands move real files in someone's cloud, on more than one
device. Read the rules before the first `--apply`.

Longer reference: [`docs/QUICKSTART_AI.md`](docs/QUICKSTART_AI.md) (output
contract, long scans), [`docs/LINUX_RCLONE_SETUP.md`](docs/LINUX_RCLONE_SETUP.md)
and [`docs/ANDROID_SETUP.md`](docs/ANDROID_SETUP.md) (setting a machine up).

## Rules

Never, without the person's explicit go-ahead for this case:

- **Run a full cloud scan to fix something.** It walks the whole disk, thousands
  of requests. Scan the folder in question: `python3 ydm.py --backend rclone scan
  cloud --path /Folder`. `sync_policy.py` suggests a full scan when no scan covers
  the disk root; on a real disk that is a question for the person, not a step.
- **`resync --apply` without reading its plan and comparing the two sides**
  (see *Compare before a resync*). A resync makes both sides the union of each
  other, and the newer file wins where they differ.
- **Edit `<mirror>.bisync.filters` or `<mirror>.download.filters` by hand.** They
  are generated from the policy, and the next render discards your edit. Use
  `ignore`, `quarantine`, `add` and `remove`.
- **Run `rclone bisync`, `sync`, `copy`, `delete`, `purge` or `moveto` on the
  mirror or the remote yourself.** Reads (`lsf`, `cat`, `md5sum`, `check`) are
  fine. If only a bypass will do, say so and say why.
- **Pass `--force-filter`, `--force-risk`, `--reset-daemon-index` or a higher
  `--max-delete`.** Each one overrides a guard that exists because something was
  lost. Report what the guard said instead.
- **Delete anything because it looks like junk.** A `.partial` file can be the
  only surviving copy. Look at it and ask.
- **Print `rclone.conf`, `rclone config show` or `.env`.** They contain tokens.

Always:

- **Pass `--backend rclone` to `sync_policy.py` on an rclone machine.** The
  default, `auto`, picks the daemon whenever one is configured, even a stopped
  one, and `--apply` then rewrites its config and restarts it. `YDM_BACKEND=rclone`
  does the same job for a whole session.
- **Run without `--apply` first.** Everything that changes state is a dry run
  until you add it.
- **Pass `--format json` and check the exit code.** See below.
- **Run bisync without a proxy.** A session that exports `https_proxy` hands it to
  rclone, and the plan warns about it. Pass `--no-proxy` to `resync` and `run`.
- **Use the rclone the timer uses.** `install_timer.sh install` records it in
  `var/rclone_binary`, so manual runs pick it up. Elsewhere, set `YDM_RCLONE` when
  the system rclone is older than 1.66: bisync's recovery flags need that version.

Do not use the `ydm` menu or the `ydm-sync-add` / `ydm-sync-rm` aliases: the menu
is for people, and the aliases apply immediately with no dry run. Never let the
daemon and bisync work on the same folder at once.

The repository is public. Tests, docs and commits use synthetic paths only: no
real file names, hosts, addresses or account details.

## Exit codes

| Code | Meaning | What to do |
|------|---------|------------|
| `0` | Done, or a dry run that found nothing wrong | Read the JSON anyway |
| `1` | Failed | Read `data.error`; see *When something fails* |
| `2` | Bad command line (argparse) | Fix the call |
| `3` | A guard refused to run (`blocked: true`): no baseline, changed filter, lock held | Nothing went wrong; do what `data.error` says, or wait for the lock |

`sync_rename.py preflight` always exits `0` once it has a decision; read the
decision from its JSON.

## Before you start

```bash
python3 tools/sync_bisync.py doctor --format json
python3 tools/sync_policy.py status --format json
```

`doctor` is one read-only summary: rclone, proxy, filter, baseline, last run,
pause, lock, timer, daemon, quarantine. Each entry in `data.checks` has a `status`
(`ok`, `warn`, `fail`, `skipped`), a `detail`, and a `next` step when it is not
`ok`. It exits `1` if any check is `fail`. `--check-network` also lists the remote.
`sync_policy.py status` shows the paths and their modes.

`YDM_LOCAL_ROOT` or `--local-root` names the mirror.

Pause the sync for anything that ends in a resync, and resume afterwards. `run`
exits `3` during a pause, on the timer and on a phone's job alike, while `resync`
keeps working. A run already in progress finishes, and `pause` warns about it.

```bash
python3 tools/sync_bisync.py pause --reason "adding /Folder" --apply --format json
python3 tools/sync_bisync.py resume --apply --format json
```

## Add a folder

```bash
python3 ydm.py --backend rclone scan cloud --path /Folder
python3 tools/sync_policy.py inspect --path /Folder --format json
python3 tools/sync_policy.py add --path /Folder --mode bidirectional --backend rclone --preflight --format json
python3 tools/sync_policy.py add --path /Folder --mode bidirectional --backend rclone --preflight --apply --format json
python3 tools/sync_bisync.py resync --format json
python3 tools/sync_bisync.py resync --apply --format json
python3 tools/sync_bisync.py run --apply --format json
```

- `inspect` needs a base scan of the whole disk. Its absence is the error *No
  cloud scan covers the disk root*; see the first rule.
- `safe_for_bidirectional: false` means use `--mode download_only`.
- `safe_for_bidirectional` does not mean the cloud will hand the files over.
  `--preflight` checks that: it reads the first bytes of every file in the
  snapshot and lists failures in `data.content_probe.unreadable`. With `--apply`
  it refuses (exit `3`) and changes nothing if any file fails. That catches a
  file the cloud refuses outright, not one that breaks halfway, and it takes a
  while on a large folder.
- If the probe found files the cloud refuses, replace `--preflight` with
  `--quarantine-unreadable`: the folder goes in with those files already set
  aside. A timeout counts as unreadable, so try once more before quarantining.
- `add --apply` renders the filters itself. `render-filters` is not a step here;
  `data.next` says what is.
- Compare before the `resync --apply` when the folder already exists on both
  sides.
- `resync --apply` can exit `1` here for two ordinary reasons: a file the cloud
  will not hand over, or a cloud that has never seen bisync. `data.next_action`
  tells them apart; see *When something fails*.

The resync is finished when it exits `0`; a `run` afterwards exits `0` as well.

## Compare before a resync

```bash
CHECK=$(mktemp)
"$YDM_RCLONE" check "$YDM_LOCAL_ROOT" yandex: \
  --filter-from "$YDM_LOCAL_ROOT.bisync.filters" --combined "$CHECK" --checkers 8
cut -c1 "$CHECK" | sort | uniq -c    # = same  + only local  - only cloud  * differ  ! error
```

`rclone check` exits `1` and counts "errors" whenever the sides differ, so `+` and
`-` lines on a new folder are not a failure. Read the file, not the exit code.

Read every `*` line: the resync settles them by date, and nobody is asked. Show
them to the person if they are not obviously yours.

## Quarantine a file the cloud will not hand over

A `resync` or `run` that stopped on such a file exits `1`, lists it in
`data.failed_paths`, and gives the commands in `data.next_action.commands`. They
are not run for you. Put what the cloud answered and the date (`YYYY-MM-DD`) in
the reason, or nobody will know when to take it out.

```bash
python3 tools/sync_policy.py quarantine --path "/Folder/file.ext" --reason "500 on download since 2026-01-31" --backend rclone --format json
python3 tools/sync_policy.py quarantine --path "/Folder/file.ext" --reason "500 on download since 2026-01-31" --backend rclone --apply --format json
python3 tools/sync_policy.py render-filters --backend rclone --apply --format json
python3 tools/sync_bisync.py resync --apply --format json
```

`quarantine` and `ignore` change the policy only, so `render-filters --apply`
is a required step for them, unlike for `add`.

List entries with `python3 tools/sync_policy.py quarantine --backend rclone
--format json`.

To put files back, ask the cloud again:

```bash
python3 tools/sync_policy.py quarantine --recheck --backend rclone --format json
python3 tools/sync_policy.py quarantine --recheck --backend rclone --apply --format json
python3 tools/sync_policy.py render-filters --backend rclone --apply --format json
python3 tools/sync_bisync.py resync --apply --format json
```

Without `--apply` it reads the first bytes of each file, which shows only that a
file has started to be served (`verified: first_bytes`). With `--apply` it fetches
each file whole, checks it against the cloud's md5, sets it in the mirror, and
only then takes it out of quarantine (`verified: md5`). The resync then does not
depend on the cloud serving it twice. `--path` limits it to one file. A local copy
that differs stays where it is, and the entry stays in quarantine for the person
to settle.

## A file the cloud will not serve, but another device has

Getting the file onto this machine is a separate job: reading another device needs
its owner's permission. Then:

```bash
python3 tools/sync_policy.py import-file --source /tmp/file.ext --path "/Folder/file.ext" --backend rclone --format json
python3 tools/sync_policy.py import-file --source /tmp/file.ext --path "/Folder/file.ext" --backend rclone --apply --format json
```

It refuses a file whose md5 differs from the cloud's and never overwrites a
different local file. It writes to the mirror only, with the cloud's date, and
`data.next` lists what is left. A quarantined file then needs `quarantine --remove`,
`render-filters` and a resync. Do not upload a replacement: that is a change to the
cloud copy, and it needs the person.

## Ignore generated files

```bash
python3 tools/sync_policy.py ignore --path /Folder --pattern build/ --pattern '*.log' --backend rclone --apply --format json
python3 tools/sync_policy.py render-filters --backend rclone --apply --format json
python3 tools/sync_bisync.py resync --apply --format json
```

A pattern is a name or a name glob, with a trailing `/` for a directory. Paths and
`**` are refused.

## Stop syncing a folder

```bash
python3 tools/sync_policy.py remove --path /Folder --backend rclone --format json
python3 tools/sync_policy.py remove --path /Folder --backend rclone --apply --format json
python3 tools/sync_bisync.py resync --apply --format json
```

The cloud copy and the local copy both stay. Quarantine entries under the path
stay in the policy too; remove them yourself if the folder is gone for good.

## Rename or move a synced file

```bash
python3 tools/sync_rename.py plan --old /Folder/a.ext --new /Folder/b.ext --format json
python3 tools/sync_rename.py apply --old /Folder/a.ext --new /Folder/b.ext --format json
```

`apply` moves the file on both sides and re-establishes the baseline itself. A
plain `mv` in the mirror reads as a delete and a new upload.

## A folder for one-way download

```bash
python3 tools/sync_policy.py add --path /Folder --mode download_only --backend rclone --apply --format json
python3 tools/sync_policy.py download-plan --format json
python3 tools/sync_policy.py download-apply --apply --format json
```

`download-plan` leaves out quarantined and ignored files and lists them in
`data.excluded`. A file that fails is named in `data.failed_paths`; the rest are
still copied. `--timeout-sec` caps the whole copy (6 hours by default). This is
for one-way paths, not for pre-loading a folder that will become bidirectional.

## When something fails

A failed `resync` or `run` carries the diagnosis in its JSON:

- `data.failed_paths`: `[{"path", "reason"}]`, the files rclone says failed. Empty
  when the log has no such line.
- `data.next_action`: `kind` (`quarantine`, `missing_sentinel`, `stop` or
  `timed_out` or `read_stderr`), `why`, and `commands`, ready to read and run
  yourself. A rule
  above still outranks a suggested command.

| `data.error` or rclone's message | Cause | Next step |
|---|---|---|
| `No successful resync on record` | No baseline on this machine | Compare, then `resync --apply` |
| `Filter-file changed since the last resync` | The policy moved on | Check why, compare, then `resync --apply` |
| `Another bisync run is already in progress` | Lock held | Wait; do not delete the lock |
| `Access test failed … Path2 count 0 - RCLONE_TEST` | The sentinel is not in the cloud | Below |
| `Failed to copy` / a 500 or 503 for one file | The cloud will not hand it over | Quarantine it |
| `Safety abort: too many deletes` (exit `1`) | One side lost more than `--max-delete` percent of the files | Stop: this is what a wiped side looks like. rclone suggests `--force`; do not take it. Report it |
| Listings hang with no transfers | Proxy in the environment | Retry with `--no-proxy`; `--timeout-sec` stops a run that hangs |
| `permission denied` on `.filters.md5` | The mirror's parent is not writable | See `docs/LINUX_RCLONE_SETUP.md` §3 |

rclone's full output is in `data.bisync.stderr` of the JSON and in
`var/bisync_last.log`; `var/bisync.log` keeps the history. On a timer machine,
`journalctl --user -u ydm-bisync.service -n 50` has the same story.

`Bisync aborted. Must run --resync to recover` is rclone's wording, not a
go-ahead. Find out why it aborted first.

**A cloud that has never seen bisync.** `--check-access` looks for `RCLONE_TEST`
on both sides before it copies anything, so the first baseline on a clean account
fails (`next_action.kind: missing_sentinel`). Confirm the cloud really is reachable
and only the sentinel is missing, then run the resync without the check:

```bash
"$YDM_RCLONE" lsf yandex: --max-depth 1
python3 tools/sync_bisync.py resync --no-check-access --apply --format json
```

The sentinel reaches the cloud only when that resync finishes. If it stops on a
file the cloud will not hand over, quarantine the file and repeat the same
command, because a plain resync would fail on the sentinel again. Once one has
finished, every later run keeps the check.

If the listing comes back empty or wrong, stop: the sentinel is missing because
the remote is, and the check is doing its job.

## Rehearse on a copy

`--policy-path` alone is not a rehearsal: state under `var/` and the daemon config
stay live. Use a copy of the code, a separate `HOME`, and an rclone `alias` remote
called `yandex` that points at a local folder:

```bash
mkdir -p "$SANDBOX"/{repo,home,cloud,mirror}
git archive HEAD | tar -x -C "$SANDBOX/repo"
export HOME="$SANDBOX/home" YDM_LOCAL_ROOT="$SANDBOX/mirror"
export RCLONE_CONFIG="$SANDBOX/home/rclone.conf"
printf '[yandex]\ntype = alias\nremote = %s\n' "$SANDBOX/cloud" > "$RCLONE_CONFIG"
```

Run everything from `$SANDBOX/repo`, with a real rclone. A file with `chmod 000`
in the fake cloud fails the way a file Yandex answers 500 for does.

## Changing YDM

- `python3 tests/test_agents_doc.py` checks that the commands and flags quoted in
  this file exist. A new or changed command updates this file in the same commit.
- `tests/test_exit_codes.py` holds new tools to the exit-code contract.
- Dry run by default, `--apply` to change state, `{"success": bool, "data": {...}}`
  for JSON, `sys.exit(main())` with `exit_code(payload)` for the code.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) has the rest; open work is in
  [`tasks/agent_interface/BACKLOG.md`](tasks/agent_interface/BACKLOG.md).
