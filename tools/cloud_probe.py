#!/usr/bin/env python3
"""Is the live-disk half of Yandex Disk's API answering again?

Written for the outage that began 2026-10-11 (docs/incidents/
yandex-disk-410-gone-2026-10-11.md): every `/v1/disk/resources*` call answered
`410 GoneError` while `/v1/disk` and the trash stayed up. `rclone about` hits
`/v1/disk`, so it says "alive" the whole time; the only honest question is one
about the resource layer. That is what this asks, and nothing else.

    python3 tools/cloud_probe.py check     # one probe, prints it, exit code says
    python3 tools/cloud_probe.py watch     # probe, remember, log, notify on change

Exit codes: 0 open, 10 closed (410), 11 could not tell, 12 the watcher itself
failed (`watch` only; the reason is the last line of var/cloud_watch.log).

It reads, never writes. Reopening is not the same as safe to sync again — the
owner's checklist in the incident note still applies — so a notification from
here says "look", and nothing here switches the sync back on.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.sync_common import var_path  # noqa: E402
from tools.trash_scan import resolve_token  # noqa: E402

API = "https://cloud-api.yandex.net/v1/disk"
OPEN, CLOSED, UNKNOWN = "open", "closed", "unknown"
AUTH = "auth"          # remembered state only: a verdict the watcher cannot reach
EXIT_CODES = {OPEN: 0, CLOSED: 10, UNKNOWN: 11}
EXIT_FAILED = 12       # the watcher itself broke; `watch` only
NOTIFY_ID = "ydm-cloud-watch"
NOTIFY_BIN = "/data/data/com.termux/files/usr/bin/termux-notification"
NOTIFY_REMOVE_BIN = "/data/data/com.termux/files/usr/bin/termux-notification-remove"


@dataclass
class Probe:
    status: str            # open | closed | unknown
    root: Optional[int]    # HTTP code of the root listing; None if no answer
    files: Optional[int]   # HTTP code of /resources/files; context, not a verdict
    auth: bool = False     # 401/403: the watcher itself cannot get in


def _code(url: str, token: str, opener) -> Optional[int]:
    request = urllib.request.Request(url, headers={
        "Authorization": f"OAuth {token}", "Accept": "application/json",
    })
    try:
        with opener(request, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def probe(token: str, opener=urllib.request.urlopen) -> Probe:
    """Ask the root listing, which is step 1 of the incident note's way back.

    `/resources/files` is asked too because the smart scan stands on it, but it
    does not decide the verdict: the question is whether the tree is readable.
    """
    root = _code(f"{API}/resources?" + urllib.parse.urlencode(
        {"path": "/", "limit": "1", "fields": "path"}), token, opener)
    files = _code(f"{API}/resources/files?limit=1", token, opener)
    if root == 200:
        return Probe(OPEN, root, files)
    if root == 410:
        return Probe(CLOSED, root, files)
    return Probe(UNKNOWN, root, files, auth=root in (401, 403))


def describe(result: Probe) -> str:
    return f"{result.status} root={result.root} files={result.files}"


# --- watch ------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_state(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as handle:
            status = json.load(handle).get("status")
    except (OSError, ValueError, AttributeError):
        return None
    return status if status in (OPEN, CLOSED, AUTH) else None


def _write_state(path: str, status: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"status": status, "checked_at": _now()}, handle)
    os.replace(tmp, path)


def watch_once(
    token_for: Callable[[], str],
    *,
    state_path: str,
    log_path: str,
    notify: Callable[[str, str], None],
    dismiss: Callable[[], None],
    refresh: Callable[[], None] = lambda: None,
    opener=urllib.request.urlopen,
) -> Probe:
    """One probe, one log line, and a card only when the picture changed.

    Four things that look like details and are the point:

    * A card is sent on the *change* to open, not on every open — the job runs
      every few hours and a card per run teaches the reader to ignore the card.
    * A card is taken back when the disk closes again: it describes a state
      that is no longer true.
    * A 401/403 is reported once, because a watcher that cannot authenticate is
      silent in exactly the way a healthy-and-closed one is.
    * A timeout or no network proves nothing about the disk, so it is logged
      and does not touch the remembered state — otherwise open, a dead phone
      signal, open would announce the same reopening twice.
    """
    result = probe(token_for(), opener)
    if result.auth:
        # rclone refreshes its own access_token, and while the sync is off
        # nothing runs rclone. Making it do one cheap call is the least that
        # can be tried before calling the watcher blind.
        refresh()
        result = probe(token_for(), opener)

    previous = _read_state(state_path)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"{_now()} {describe(result)}"
                  f"{' auth' if result.auth else ''}\n")

    if result.status == UNKNOWN and not result.auth:
        return result

    state = AUTH if result.auth else result.status
    if state != previous:
        if result.status == OPEN:
            notify(
                "Яндекс.Диск снова отвечает",
                f"Корень: {result.root}, files: {result.files}. Синк не включён: "
                "сначала сверка по docs/incidents/yandex-disk-410-gone-2026-10-11.md",
            )
        elif result.auth:
            notify(
                "Проверка Яндекс.Диска не может войти",
                f"Ответ {result.root} на токен. Пока это не поправлено, "
                "проверка не скажет, открылся ли диск.",
            )
        else:
            dismiss()
        _write_state(state_path, state)
    return result


# --- command line -----------------------------------------------------------

def _run(binary: str, args: list) -> None:
    if not os.access(binary, os.X_OK):
        return
    try:
        subprocess.run([binary, *args], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def _make_notify():
    binary = os.environ.get("YDM_NOTIFY_BIN", NOTIFY_BIN)
    remove = os.environ.get("YDM_NOTIFY_REMOVE_BIN", NOTIFY_REMOVE_BIN)

    def notify(title: str, content: str) -> None:
        _run(binary, ["--title", title, "--content", content,
                      "--id", NOTIFY_ID, "--alert-once"])

    def dismiss() -> None:
        _run(remove, [NOTIFY_ID])

    return notify, dismiss


def _make_refresh(remote: str):
    def refresh() -> None:
        try:
            subprocess.run(["rclone", "about", f"{remote}:"],
                           capture_output=True, timeout=90)
        except (OSError, subprocess.SubprocessError):
            pass
    return refresh


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["check", "watch"])
    parser.add_argument("--remote", default="yandex")
    parser.add_argument(
        "--token-source", default="rclone", choices=["auto", "env", "rclone"],
        help="rclone by default: it refreshes its own token, a copy in .env may not",
    )
    args = parser.parse_args(argv)

    def token_for() -> str:
        return resolve_token(args.token_source, args.remote)

    if args.command == "check":
        result = probe(token_for())
        print(describe(result))
        return EXIT_CODES[result.status]

    notify, dismiss = _make_notify()
    log_path = var_path("cloud_watch.log")
    try:
        result = watch_once(
            token_for,
            state_path=var_path("cloud_watch.json"),
            log_path=log_path,
            notify=notify, dismiss=dismiss,
            refresh=_make_refresh(args.remote),
        )
    except (Exception, SystemExit) as exc:
        # The job runs with its output thrown away, so a crash here would leave
        # a log that simply stops — indistinguishable from a disk that is still
        # closed. resolve_token ends in SystemExit when the rclone remote is
        # missing, which is exactly the case worth a line.
        reason = exc.code if isinstance(exc, SystemExit) else f"{type(exc).__name__}: {exc}"
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as log:
                log.write(f"{_now()} error {reason}\n")
        except OSError:
            pass
        print(f"error {reason}")
        return EXIT_FAILED
    print(describe(result))
    return EXIT_CODES[result.status]


if __name__ == "__main__":
    sys.exit(main())
