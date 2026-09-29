"""
Folder sync storage backend for sendfiledrop.
Manages synced folders, manifests, and file operations.
Zero-dependency, pure stdlib.
"""

import os
import json
import hashlib
import time
import tempfile
import threading
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

SYNCED_DIR = "synced"
TRASH_DIR = os.path.join(SYNCED_DIR, ".trash")
SYNC_METADATA_FILE = "sync.json"
RETENTION_DAYS = 30

_sync_lock = threading.RLock()


def ensure_dirs():
    """Create synced and trash directories if they don't exist."""
    os.makedirs(SYNCED_DIR, exist_ok=True)
    os.makedirs(TRASH_DIR, exist_ok=True)


def safe_join(base: str, rel: str) -> Optional[str]:
    """
    Safely join base path with a relative path.
    Rejects absolute paths, .., and symlink escapes.
    Returns None if path is unsafe.
    """
    if not base or not rel:
        return None

    base_path = Path(base).resolve()

    try:
        target = (base_path / rel).resolve()
    except (ValueError, OSError):
        return None

    if not str(target).startswith(str(base_path) + os.sep) and target != base_path:
        return None

    return str(target)


def sha256_file(path: str, chunk_size: int = 65536) -> str:
    """Compute SHA256 hash of a file."""
    h = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except (OSError, IOError):
        return ""


def load_sync_metadata() -> Dict[str, Any]:
    """Load sync.json metadata."""
    if not os.path.exists(SYNC_METADATA_FILE):
        return {"folders": {}, "next_folder_id": 1}

    try:
        with open(SYNC_METADATA_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"folders": {}, "next_folder_id": 1}


def save_sync_metadata(metadata: Dict[str, Any]):
    """Save sync.json metadata atomically."""
    with _sync_lock:
        temp_fd, temp_path = tempfile.mkstemp(dir=".", prefix=".sync_tmp_")
        try:
            with os.fdopen(temp_fd, 'w') as f:
                json.dump(metadata, f, indent=2)
            os.replace(temp_path, SYNC_METADATA_FILE)
        except (IOError, OSError):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise


def create_folder(name: str) -> Tuple[Optional[str], str]:
    """
    Create a new sync folder.
    Returns (folder_id, error_message).
    """
    with _sync_lock:
        ensure_dirs()
        metadata = load_sync_metadata()

        if not name or not isinstance(name, str) or len(name) > 255:
            return None, "Invalid folder name"

        folder_id = f"folder_{metadata['next_folder_id']}"
        metadata["next_folder_id"] += 1

        folder_path = os.path.join(SYNCED_DIR, folder_id)
        os.makedirs(folder_path, exist_ok=True)

        metadata["folders"][folder_id] = {
            "name": name,
            "created": int(time.time()),
            "manifest": {},
            "next_rev": 1
        }

        save_sync_metadata(metadata)
        return folder_id, ""


def delete_folder(folder_id: str) -> str:
    """
    Delete a sync folder (move to trash).
    Returns error message or "" on success.
    """
    with _sync_lock:
        metadata = load_sync_metadata()

        if folder_id not in metadata["folders"]:
            return "Folder not found"

        folder_path = os.path.join(SYNCED_DIR, folder_id)
        trash_path = os.path.join(TRASH_DIR, f"{folder_id}_{int(time.time())}")

        try:
            if os.path.exists(folder_path):
                os.rename(folder_path, trash_path)

            del metadata["folders"][folder_id]
            save_sync_metadata(metadata)
            return ""
        except (OSError, IOError) as e:
            return f"Failed to delete folder: {e}"


def get_folders() -> Dict[str, Dict[str, Any]]:
    """Get list of all sync folders with stats."""
    metadata = load_sync_metadata()
    result = {}

    for folder_id, info in metadata["folders"].items():
        manifest = info.get("manifest", {})
        file_count = len([f for f in manifest.values() if not f.get("deleted")])

        result[folder_id] = {
            "name": info["name"],
            "created": info["created"],
            "file_count": file_count,
            "last_modified": max(
                [f.get("mtime", 0) for f in manifest.values()],
                default=info["created"]
            )
        }

    return result


def get_manifest(folder_id: str) -> Tuple[Optional[Dict], str]:
    """
    Get folder manifest.
    Returns (manifest_dict, error_message).
    Manifest format: {path: {size, mtime, sha256, rev, deleted, device}}
    """
    metadata = load_sync_metadata()

    if folder_id not in metadata["folders"]:
        return None, "Folder not found"

    return metadata["folders"][folder_id].get("manifest", {}), ""


def write_file(
    folder_id: str,
    rel_path: str,
    file_obj,
    base_rev: Optional[int] = None,
    expected_length: Optional[int] = None,
    device: str = "host"
) -> Tuple[bool, str, int]:
    """
    Write a file to a sync folder.
    Checks base_rev for conflict detection.
    Validates complete upload with expected_length.
    Returns (success, error_message, new_rev).
    """
    with _sync_lock:
        ensure_dirs()
        metadata = load_sync_metadata()

        if folder_id not in metadata["folders"]:
            return False, "Folder not found", 0

        folder_path = os.path.join(SYNCED_DIR, folder_id)
        safe_path = safe_join(folder_path, rel_path)

        if not safe_path:
            return False, "Invalid path", 0

        folder_info = metadata["folders"][folder_id]
        manifest = folder_info.get("manifest", {})

        entry = manifest.get(rel_path, {})
        current_rev = entry.get("rev", 0)

        if base_rev is not None and base_rev != current_rev:
            return False, "Conflict: base revision mismatch", current_rev

        try:
            os.makedirs(os.path.dirname(safe_path), exist_ok=True)

            temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(safe_path))
            try:
                bytes_written = 0
                while True:
                    chunk = file_obj.read(65536)
                    if not chunk:
                        break
                    os.write(temp_fd, chunk)
                    bytes_written += len(chunk)
                os.close(temp_fd)
            except Exception:
                os.close(temp_fd)
                os.unlink(temp_path)
                raise

            if expected_length is not None and bytes_written != expected_length:
                os.unlink(temp_path)
                return False, f"Upload incomplete: expected {expected_length} bytes, got {bytes_written}", 0

            os.replace(temp_path, safe_path)

            file_size = os.path.getsize(safe_path)
            file_mtime = int(os.path.getmtime(safe_path))
            file_hash = sha256_file(safe_path)

            new_rev = folder_info.get("next_rev", 1)
            folder_info["next_rev"] = new_rev + 1

            manifest[rel_path] = {
                "size": file_size,
                "mtime": file_mtime,
                "sha256": file_hash,
                "rev": new_rev,
                "deleted": False,
                "device": device
            }

            save_sync_metadata(metadata)
            return True, "", new_rev

        except (OSError, IOError) as e:
            return False, f"Failed to write file: {e}", 0


def read_file(folder_id: str, rel_path: str) -> Tuple[Optional[str], str]:
    """
    Get absolute path to a file in a sync folder (for reading).
    Returns (file_path, error_message).
    """
    metadata = load_sync_metadata()

    if folder_id not in metadata["folders"]:
        return None, "Folder not found"

    folder_path = os.path.join(SYNCED_DIR, folder_id)
    safe_path = safe_join(folder_path, rel_path)

    if not safe_path or not os.path.isfile(safe_path):
        return None, "File not found"

    return safe_path, ""


def delete_file(folder_id: str, rel_path: str, base_rev: Optional[int] = None, device: str = "host") -> Tuple[bool, str]:
    """
    Delete a file in a sync folder (mark as deleted in manifest).
    Returns (success, error_message).
    """
    with _sync_lock:
        metadata = load_sync_metadata()

        if folder_id not in metadata["folders"]:
            return False, "Folder not found"

        folder_path = os.path.join(SYNCED_DIR, folder_id)
        safe_path = safe_join(folder_path, rel_path)

        if not safe_path:
            return False, "Invalid path"

        folder_info = metadata["folders"][folder_id]
        manifest = folder_info.get("manifest", {})
        entry = manifest.get(rel_path, {})
        current_rev = entry.get("rev", 0)

        if base_rev is not None and base_rev != current_rev:
            return False, "Conflict: base revision mismatch"

        try:
            if os.path.exists(safe_path):
                os.unlink(safe_path)

            new_rev = folder_info.get("next_rev", 1)
            folder_info["next_rev"] = new_rev + 1

            manifest[rel_path] = {
                "deleted": True,
                "rev": new_rev,
                "device": device,
                "mtime": int(time.time())
            }

            save_sync_metadata(metadata)
            return True, ""

        except (OSError, IOError) as e:
            return False, f"Failed to delete file: {e}"


def cleanup_trash():
    """Remove files from trash older than RETENTION_DAYS."""
    ensure_dirs()
    cutoff_time = time.time() - (RETENTION_DAYS * 86400)

    try:
        for item in os.listdir(TRASH_DIR):
            item_path = os.path.join(TRASH_DIR, item)
            if os.path.getmtime(item_path) < cutoff_time:
                if os.path.isdir(item_path):
                    import shutil
                    shutil.rmtree(item_path, ignore_errors=True)
                else:
                    os.unlink(item_path)
    except (OSError, IOError):
        pass
