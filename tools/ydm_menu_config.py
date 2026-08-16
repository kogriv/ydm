#!/usr/bin/env python3
"""Configuration for YDM interactive menu."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from tools.sync_backends import BackendError, detect_backend
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
    #: "daemon"/"rclone", or "" when neither backend is available on this host.
    backend_kind: str = ""
    #: Why no backend could be resolved, for display. None when one was.
    backend_error: str | None = None
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
        # A host with neither the daemon nor an rclone remote must still get a
        # usable menu that says so, not an import-time traceback.
        backend_error = None
        try:
            resolved_backend = detect_backend(
                explicit=backend,
                env=os.environ.get("YDM_BACKEND"),
                db_path=resolved_db,
                local_root=resolved_local,
                remote=resolved_remote,
                policy_path=resolved_policy,
                bisync_filter_path=resolved_bisync,
            )
            backend_name = resolved_backend.name()
            backend_kind = resolved_backend.kind
        except BackendError as exc:
            backend_error = str(exc)
            backend_name = "none available"
            backend_kind = ""
        return cls(
            db_path=resolved_db,
            local_root=resolved_local,
            policy_path=resolved_policy,
            bisync_filter_path=resolved_bisync,
            remote=resolved_remote,
            backend_name=backend_name,
            backend_kind=backend_kind,
            backend_error=backend_error,
            plain=plain,
            width=72,
        )
