# Syncing a Linux desktop with rclone bisync

The same arrangement the phone uses (`docs/ANDROID_SETUP.md`), on a desktop: a
local mirror kept in step with Yandex Disk by `rclone bisync` on a timer,
instead of the official `yandex-disk` daemon.

## Why not the daemon

The daemon (`yandex-disk` 0.1.6.1080, the newest Yandex publishes for Linux)
carries its own list of trusted certificate authorities. On 2026-10-29 Yandex's
storage host served a certificate chain from GlobalSign GCC R46 (2025) that the
daemon does not trust: every download redirected to `mds.storage.yandex.net`
failed with `certificate verify failed`, while the system's OpenSSL accepted the
same chain. Nothing on this side can update that list.

The daemon also syncs by exclusion ("everything except"), which is how a
wrongly computed exclude list once un-excluded `/Books` — see
`docs/incidents/yandex-books-delete-2026-08-14.md`. A bisync filter names what
is synced, and everything else is out.

What it costs: sync is not instant. A change reaches the cloud on the next run,
every 30 minutes by default.

## 1. A recent rclone, for ydm only

bisync before rclone 1.66 aborts the whole run on a single file the cloud
refuses to hand over, and then demands `--resync`. Distribution packages lag
(Ubuntu 24.04 ships 1.60). Other projects on the same machine use rclone for
copies and mounts, so ydm gets its own binary by path rather than replacing
theirs:

```bash
V=v1.75.2
cd "$(mktemp -d)"
curl -fLO https://downloads.rclone.org/$V/rclone-$V-linux-amd64.zip
curl -fLO https://downloads.rclone.org/$V/SHA256SUMS
grep "rclone-$V-linux-amd64.zip" SHA256SUMS | sha256sum -c -
curl -fsS https://rclone.org/KEYS | gpg --import && gpg --verify SHA256SUMS
unzip -q rclone-$V-linux-amd64.zip
install -D -m 755 rclone-$V-linux-amd64/rclone ~/.local/opt/rclone-$V/rclone
export YDM_RCLONE=~/.local/opt/rclone-$V/rclone
```

Every ydm tool runs `$YDM_RCLONE` when it is set and `rclone` from `PATH`
otherwise. With 1.66 or newer, bisync is given `--resilient --recover
--max-lock 2m`; with anything older it gets the command it always got.

## 2. An OAuth application of your own

Since 2026-10-11 Yandex refuses rclone's shared OAuth application on the
resource API: `/v1/disk` answers, every path answers `410 GoneError`, and
re-authorising through the same application gives the same token back. Register
an application at <https://oauth.yandex.ru/> with Disk access
(`cloud_api:disk.info`, `.read`, `.write`) and the redirect URI
`http://127.0.0.1:53682/`, then:

```bash
"$YDM_RCLONE" config create yandex yandex client_id=… client_secret=… config_is_local=true
```

It prints a `http://127.0.0.1:53682/auth?state=…` link and waits. Open it in a
browser **on the same machine**. Working over SSH, forward the port first from
the machine you sit at:

```bash
ssh -N -L 53682:127.0.0.1:53682 <this-host>
```

and open the link there. Do not print the remote afterwards (`config show`, the
tail of the creation output): it contains the token and the client secret.

## 3. The policy: what is synced

Build it with `sync_policy.py add`, one folder at a time; each `add` checks the
folder against a cloud scan first, so scan what you add:

```bash
python3 ydm.py scan cloud --path /pro
python3 tools/sync_policy.py add --path /pro --mode bidirectional --backend rclone --apply
```

Two further kinds of entry keep a bisync between several devices quiet:

- **ignore** — generated files under a synced path, which every device makes
  for itself and which would otherwise travel back and forth on every run:

  ```bash
  python3 tools/sync_policy.py ignore --path /pro/physon \
    --pattern build/ --pattern __pycache__/ --pattern '*.log' --pattern '*.aux' \
    --backend rclone --apply
  ```

- **quarantine** — a file the cloud lists but will not hand over (Yandex
  answering 500/503 for it), set aside with a reason until it can be read:

  ```bash
  python3 tools/sync_policy.py quarantine --path "/Books/…/file.mp3" \
    --reason "503 on download since 2026-10-29" --backend rclone --apply
  python3 tools/sync_policy.py quarantine --backend rclone        # list
  python3 tools/sync_policy.py quarantine --path "/Books/…/file.mp3" --remove --backend rclone --apply
  ```

Both live in the policy, not in the filter file: `render-filters` rewrites the
filter from the policy, and anything added to the filter by hand is gone after
the next rewrite. Then:

```bash
python3 tools/sync_policy.py render-filters --backend rclone --apply
```

Any change to the filter means a new baseline (`resync`) before the next run.

## 4. Before the first resync: compare, do not trust

`resync` makes the two sides the union of each other, and where a file differs
it keeps the newer one. Know what it will do before it does it:

```bash
"$YDM_RCLONE" check /data/ya_disk yandex: \
  --filter-from /data/ya_disk.bisync.filters --combined /tmp/check.txt --checkers 8
cut -c1 /tmp/check.txt | sort | uniq -c    # = same  + only local  - only cloud  * differ  ! error
```

`*` lines are the ones to read: a resync settles them, not you.

## 5. Switching over from the daemon

The two must never run on the same folder at once.

```bash
yandex-disk stop
# keep it from coming back at login (Hidden=true is the XDG way to switch an
# autostart entry off; systemd's autostart generator honours it):
grep -q '^Hidden=' ~/.config/autostart/Yandex.Disk.desktop \
  || echo 'Hidden=true' >> ~/.config/autostart/Yandex.Disk.desktop
python3 tools/sync_bisync.py resync --local-root /data/ya_disk            # plan
python3 tools/sync_bisync.py resync --local-root /data/ya_disk --apply
bash tools/linux/install_timer.sh install
```

The daemon's own state (`/data/ya_disk/.sync`) stays where it is; the bisync
filter does not include it.

## 6. The timer

`tools/linux/install_timer.sh` writes a systemd **user** service and timer for
this checkout and this rclone (`render` prints them without installing). The
service runs `sync_bisync.py run --apply` and nothing else: it never
establishes a baseline, and refuses to sync when the filter changed since the
last resync. When a run is blocked or fails, a desktop notification says so —
once per problem, not once per run.

```bash
YDM_RCLONE=~/.local/opt/rclone-v1.75.2/rclone bash tools/linux/install_timer.sh install
bash tools/linux/install_timer.sh status
journalctl --user -u ydm-bisync.service -n 50
```

## Going back to the daemon

```bash
bash tools/linux/install_timer.sh remove
sed -i '/^Hidden=true$/d' ~/.config/autostart/Yandex.Disk.desktop
yandex-disk start
```

The daemon's index was kept, but the mirror changed under it while it was
stopped; if it remembers paths that are now missing locally, `sync_policy.py`
refuses its restart and offers `--reset-daemon-index` — see the August incident.
