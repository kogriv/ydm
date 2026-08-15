#!/usr/bin/env python3
"""Configuration for YDM interactive menu."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from tools.sync_backends import detect_backend
from tools.sync_policy import bisync_filter_path as policy_bisync_filter_path, default_policy_path
from ydm import DEFAULT_CONFIG

ROOT_DIR = Path(__file__).resolve().parents[1]


@dataclass
class MenuConfig:
    db_path: str
    local_root: str
    policy_path: str
    bisync_filter_path: str
    remote: str
    backend_name: str
    backend_kind: str = "daemon"
    plain: bool = False
    width: int = 72

    @classmethod
    def from_env_and_args(
        cls,
        *,
        db_path: str | None = None,
        local_root: str | None = None,
        policy_path: str | None = None,
        bisync_filter_path: str | None = None,
        remote: str | None = None,
        backend: str | None = None,
        plain: bool = False,
    ) -> "MenuConfig":
        resolved_local = os.path.expanduser(
            local_root
            or os.environ.get("YDM_LOCAL_ROOT")
            or DEFAULT_CONFIG["local_root"]
        )
        resolved_db = os.path.expanduser(
            db_path
            or os.environ.get("YDM_DB")
            or str(ROOT_DIR / "monitor.db")
        )
        resolved_policy = os.path.expanduser(
            policy_path
            or os.environ.get("YDM_POLICY")
            or default_policy_path()
        )
        resolved_bisync = os.path.expanduser(
            bisync_filter_path
            or os.environ.get("YDM_BISYNC_FILTER")
            or policy_bisync_filter_path(resolved_local)
        )
        resolved_remote = remote or os.environ.get("YDM_REMOTE") or DEFAULT_CONFIG["rclone_remote"]
        resolved_backend = detect_backend(
            explicit=backend,
            env=os.environ.get("YDM_BACKEND"),
            db_path=resolved_db,
            local_root=resolved_local,
            remote=resolved_remote,
            policy_path=resolved_policy,
            bisync_filter_path=resolved_bisync,
        )
        return cls(
            db_path=resolved_db,
            local_root=resolved_local,
            policy_path=resolved_policy,
            bisync_filter_path=resolved_bisync,
            remote=resolved_remote,
            backend_name=resolved_backend.name(),
            backend_kind=resolved_backend.kind,
            plain=plain,
            width=72,
        )
