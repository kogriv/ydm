"""Offline configuration and resolution for isolated YDM targets.

This module deliberately does not import cloud clients or create directories.
It is safe to use from ``ydm targets`` validation commands and in tests.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Optional
import json
import os
import re


SCHEMA = "ydm_targets:v1"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_TARGET_KEYS = {
    "provider", "inventory", "sync", "local_root", "state_dir",
    "config_profile", "native_docs",
}
_INVENTORY_KEYS = {"kind", "remote"}
_SYNC_KEYS = {"kind", "remote"}


class TargetConfigError(ValueError):
    """A config error whose message identifies the invalid JSON path."""


def _error(path: str, message: str) -> TargetConfigError:
    return TargetConfigError(f"{path}: {message}")


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise _error(path, "must be an object")
    return value


def _keys(value: Mapping[str, Any], allowed: set[str], path: str) -> None:
    for key in value:
        if key not in allowed:
            raise _error(f"{path}.{key}", "unknown key")


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise _error(path, "must be a non-empty string")
    return value


def _path(value: Any, path: str) -> str:
    raw = _string(value, path)
    if not os.path.isabs(raw):
        raise _error(path, "must be an absolute path")
    return os.path.normpath(os.path.expanduser(raw))


@dataclass(frozen=True)
class TargetConfig:
    """Validated target definition from a ``ydm_targets:v1`` document."""

    name: str
    provider: str
    inventory_kind: str
    inventory_remote: Optional[str]
    sync_kind: str
    sync_remote: Optional[str]
    local_root: str
    state_dir: str
    config_profile: str
    native_docs: str = "metadata"

    @property
    def remote(self) -> Optional[str]:
        """The inventory remote, or the sync remote when inventory has none."""
        return self.inventory_remote or self.sync_remote


@dataclass(frozen=True)
class TargetContext:
    """Immutable resolved target plus every target-scoped runtime path."""

    config: TargetConfig
    source: str
    db_path: str
    policy_path: str
    download_filter_path: str
    bisync_filter_path: str
    bisync_state_path: str
    bisync_log_path: str
    rename_state_path: str
    locks_dir: str
    lock_path: str

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def provider(self) -> str:
        return self.config.provider

    @property
    def inventory_kind(self) -> str:
        return self.config.inventory_kind

    @property
    def sync_kind(self) -> str:
        return self.config.sync_kind

    @property
    def local_root(self) -> str:
        return self.config.local_root

    @property
    def state_dir(self) -> str:
        return self.config.state_dir


@dataclass(frozen=True)
class TargetsFile:
    path: str
    default_target: Optional[str]
    targets: Mapping[str, TargetConfig]


def _component(value: Any, path: str, allowed_kinds: set[str]) -> tuple[str, Optional[str]]:
    component = _mapping(value, path)
    _keys(component, _INVENTORY_KEYS if path.endswith("inventory") else _SYNC_KEYS, path)
    kind = _string(component.get("kind"), f"{path}.kind")
    if kind not in allowed_kinds:
        raise _error(f"{path}.kind", f"must be one of {', '.join(sorted(allowed_kinds))}")
    remote = component.get("remote")
    if kind == "rclone":
        remote = _string(remote, f"{path}.remote")
    elif remote is not None:
        raise _error(f"{path}.remote", "is only valid when kind is rclone")
    return kind, remote


def _parse_target(name: str, raw: Any) -> TargetConfig:
    path = f"$.targets.{name}"
    if not _NAME.fullmatch(name):
        raise _error(path, "target name must contain only letters, digits, _ or -")
    value = _mapping(raw, path)
    _keys(value, _TARGET_KEYS, path)
    required = ("provider", "inventory", "sync", "local_root", "state_dir", "config_profile")
    for key in required:
        if key not in value:
            raise _error(f"{path}.{key}", "missing required key")
    provider = _string(value["provider"], f"{path}.provider")
    if provider not in {"yandex", "google_drive"}:
        raise _error(f"{path}.provider", "must be yandex or google_drive")
    inventory_kind, inventory_remote = _component(
        value["inventory"], f"{path}.inventory", {"yandex_api", "rclone"}
    )
    sync_kind, sync_remote = _component(value["sync"], f"{path}.sync", {"daemon", "rclone"})
    if provider == "google_drive" and inventory_kind != "rclone":
        raise _error(
            f"{path}.inventory.kind",
            "google_drive requires rclone inventory",
        )
    if provider == "google_drive" and sync_kind != "rclone":
        raise _error(
            f"{path}.sync.kind",
            "google_drive requires rclone sync",
        )
    native_docs = value.get("native_docs", "metadata")
    if native_docs not in {"metadata", "export", "skip"}:
        raise _error(f"{path}.native_docs", "must be metadata, export, or skip")
    return TargetConfig(
        name=name, provider=provider, inventory_kind=inventory_kind,
        inventory_remote=inventory_remote, sync_kind=sync_kind,
        sync_remote=sync_remote, local_root=_path(value["local_root"], f"{path}.local_root"),
        state_dir=_path(value["state_dir"], f"{path}.state_dir"),
        config_profile=_string(value["config_profile"], f"{path}.config_profile"),
        native_docs=native_docs,
    )


def load_targets_file(path: str) -> TargetsFile:
    """Load and validate a targets document without any network or writes."""
    resolved = os.path.abspath(os.path.expanduser(path))
    try:
        with open(resolved, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        raise
    except json.JSONDecodeError as exc:
        raise _error("$", f"invalid JSON: {exc.msg}") from exc
    root = _mapping(raw, "$")
    _keys(root, {"schema", "default_target", "targets"}, "$")
    if root.get("schema") != SCHEMA:
        raise _error("$.schema", f"must equal {SCHEMA}")
    if "targets" not in root:
        raise _error("$.targets", "missing required key")
    raw_targets = _mapping(root["targets"], "$.targets")
    if not raw_targets:
        raise _error("$.targets", "must not be empty")
    targets = {name: _parse_target(name, value) for name, value in raw_targets.items()}
    target_items = list(targets.values())
    for index, left in enumerate(target_items):
        for right in target_items[index + 1:]:
            try:
                shared = os.path.commonpath((left.state_dir, right.state_dir))
            except ValueError:
                # Different drives on Windows cannot overlap.
                continue
            if shared in {left.state_dir, right.state_dir}:
                raise _error(
                    f"$.targets.{right.name}.state_dir",
                    f"must not equal or be nested with $.targets.{left.name}.state_dir",
                )
    default_target = root.get("default_target")
    if default_target is not None:
        default_target = _string(default_target, "$.default_target")
        if default_target not in targets:
            raise _error("$.default_target", "must name an entry in $.targets")
    return TargetsFile(resolved, default_target, MappingProxyType(targets))


def discover_targets_file(explicit_path: Optional[str] = None, environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Return the selected targets file, or None for legacy mode.

    An explicit CLI path wins over ``YDM_TARGETS_FILE``; the repository-local
    conventional filename is used only when it exists.
    """
    env = os.environ if environ is None else environ
    candidate = explicit_path or env.get("YDM_TARGETS_FILE")
    if candidate:
        return os.path.abspath(os.path.expanduser(candidate))
    conventional = os.path.abspath("ydm_targets.json")
    return conventional if os.path.exists(conventional) else None


def context_for(config: TargetConfig, source: str) -> TargetContext:
    state = config.state_dir
    return TargetContext(
        config=config, source=source,
        db_path=os.path.join(state, "monitor.db"),
        policy_path=os.path.join(state, "sync_policy.json"),
        download_filter_path=os.path.join(state, "download.filters"),
        bisync_filter_path=os.path.join(state, "bisync.filters"),
        bisync_state_path=os.path.join(state, "bisync_state.json"),
        bisync_log_path=os.path.join(state, "bisync.log"),
        rename_state_path=os.path.join(state, "rename_state.json"),
        locks_dir=os.path.join(state, "locks"),
        lock_path=os.path.join(state, "locks", "cloud_scan.lock"),
    )


def resolve_target(*, cli_target: Optional[str] = None, targets_path: Optional[str] = None,
                   environ: Optional[Mapping[str, str]] = None) -> Optional[TargetContext]:
    """Resolve CLI → environment → config default, or return legacy ``None``."""
    env = os.environ if environ is None else environ
    path = discover_targets_file(targets_path, env)
    selected = cli_target or env.get("YDM_TARGET")
    if path is None:
        if selected:
            raise TargetConfigError("--target: targets file is not configured")
        return None
    document = load_targets_file(path)
    source = "cli" if cli_target else "env" if env.get("YDM_TARGET") else "default"
    selected = selected or document.default_target
    if not selected:
        raise TargetConfigError("$.default_target: required when no --target or YDM_TARGET is set")
    if selected not in document.targets:
        raise TargetConfigError(f"--target {selected}: target is not defined in {path}")
    return context_for(document.targets[selected], source)
