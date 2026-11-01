#!/usr/bin/env python3
"""Print an `exclude-dirs=` line that leaves only the given root folders synced.

For the yandex-disk daemon's config: every folder in the disk root except those
named with --keep. It reads the token from `YANDEX_DISK_TOKEN` or `.env` and never
from a command-line argument, where it would land in the shell history and in
`ps`. It used to take the first argument as the token, so `--help` went out to
the API as one.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ydm import load_token_from_env  # noqa: E402

API_URL = ("https://cloud-api.yandex.net/v1/disk/resources?path=/&limit=1000"
           "&fields=_embedded.items.name,_embedded.items.type")
SCHEMA = "ydm_exclude_list:v1"


def root_folders(token: str) -> list:
    request = urllib.request.Request(API_URL, headers={
        "Authorization": f"OAuth {token}", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        items = json.loads(response.read().decode("utf-8")).get("_embedded", {}).get("items", [])
    return [item["name"] for item in items if item["type"] == "dir"]


def build(keep: list) -> dict:
    payload = {"schema": SCHEMA, "action": "gen-exclude-list", "dry_run": True, "keep": keep,
               "error": None}
    token = os.environ.get("YANDEX_DISK_TOKEN") or load_token_from_env()
    if not token:
        payload["error"] = "No token: set YANDEX_DISK_TOKEN, or put it in .env"
        return payload
    try:
        folders = root_folders(token)
    except urllib.error.HTTPError as exc:
        hint = ": check the token" if exc.code == 401 else ""
        payload["error"] = f"Yandex Disk answered {exc.code} {exc.reason}{hint}"
        return payload
    except (OSError, ValueError) as exc:
        payload["error"] = f"Could not reach Yandex Disk: {exc}"
        return payload
    excluded = sorted(set(folders) - set(keep))
    payload.update(root_folders=len(folders), exclude=excluded,
                   exclude_dirs_line=f"exclude-dirs={','.join(excluded)}")
    return payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", action="append", default=None, metavar="FOLDER",
                        help="a root folder to keep synced; repeat for several (default: pro)")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)
    payload = build(args.keep or ["pro"])
    if args.format == "json":
        print(json.dumps({"success": payload["error"] is None, "data": payload},
                         ensure_ascii=False, indent=2))
    elif payload["error"]:
        print(f"ERROR: {payload['error']}", file=sys.stderr)
    elif payload["exclude"]:
        print(payload["exclude_dirs_line"])
        print(f"root folders: {payload['root_folders']}, excluded: {len(payload['exclude'])}, "
              f"kept: {', '.join(args.keep or ['pro'])}", file=sys.stderr)
    else:
        print("Nothing to exclude: every root folder is kept.", file=sys.stderr)
    return 1 if payload["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
