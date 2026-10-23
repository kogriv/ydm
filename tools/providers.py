"""Provider semantics kept separate from inventory transport and sync engines."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class Capabilities:
    stable_object_id: bool
    duplicate_sibling_names: bool
    content_hashes: frozenset[str]
    native_documents: bool
    shortcuts: bool
    revisions: bool
    trash_listing: bool
    server_side_move: bool
    bidirectional_sync: bool


class ProviderAdapter:
    name = ""
    capabilities: Capabilities

    def normalize_rclone_item(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        """Preserve raw rclone identity/metadata while adding provider semantics."""
        is_dir = bool(entry.get("IsDir", False))
        hashes = dict(entry.get("Hashes") or {})
        hash_name = "md5" if hashes.get("md5") else next(iter(sorted(hashes)), None)
        metadata = dict(entry.get("Metadata") or {})
        if hashes:
            metadata["rclone_hashes"] = hashes
        return {
            "name": entry["Name"],
            "type": "dir" if is_dir else "file",
            "size": 0 if is_dir else entry.get("Size"),
            "md5": hashes.get("md5"),
            "object_id": entry.get("ID"),
            "parent_id": entry.get("ParentID"),
            "mime_type": entry.get("MimeType"),
            "hash_name": hash_name,
            "content_hash": hashes.get(hash_name) if hash_name else None,
            "is_native": False,
            "is_shortcut": False,
            "metadata": metadata,
        }


class YandexProvider(ProviderAdapter):
    name = "yandex"
    capabilities = Capabilities(
        stable_object_id=False, duplicate_sibling_names=False,
        content_hashes=frozenset({"md5"}), native_documents=False,
        shortcuts=False, revisions=True, trash_listing=True,
        server_side_move=True, bidirectional_sync=True,
    )


class GoogleDriveProvider(ProviderAdapter):
    name = "google_drive"
    capabilities = Capabilities(
        stable_object_id=True, duplicate_sibling_names=True,
        content_hashes=frozenset(), native_documents=True, shortcuts=True,
        revisions=False, trash_listing=False, server_side_move=False,
        bidirectional_sync=False,
    )

    def normalize_rclone_item(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        item = super().normalize_rclone_item(entry)
        mime_type = item["mime_type"] or ""
        is_shortcut = bool(entry.get("IsShortcut", False)) or mime_type == "application/vnd.google-apps.shortcut"
        is_native = (
            mime_type.startswith("application/vnd.google-apps.")
            and mime_type not in {"application/vnd.google-apps.folder", "application/vnd.google-apps.shortcut"}
        ) or (not item["type"] == "dir" and entry.get("Size") is not None and entry.get("Size") < 0)
        if is_shortcut:
            item["type"] = "shortcut"
        item["is_shortcut"] = is_shortcut
        item["is_native"] = is_native
        # Native docs are metadata objects: Size=-1 is meaningful and must not
        # be converted to a zero-byte regular file.
        if is_native:
            item["size"] = None
        return item


_PROVIDERS = {"yandex": YandexProvider(), "google_drive": GoogleDriveProvider()}


def provider_for(name: str) -> ProviderAdapter:
    try:
        return _PROVIDERS[name]
    except KeyError as exc:
        raise ValueError(f"unknown provider: {name}") from exc
