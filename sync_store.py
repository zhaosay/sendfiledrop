"""
Folder sync storage backend for sendfiledrop.
Manages synced folders, manifests, and file operations.
元数据存在 sqlite（sync.db）：按行增量更新，不再整份重写 JSON。
Zero-dependency, pure stdlib.
"""

import os
import json
import hashlib
import sqlite3
import time
import tempfile
import threading
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

# SFD_DATA_DIR 可指定数据目录（测试隔离用），默认沿用当前工作目录
_DATA_DIR = os.environ.get("SFD_DATA_DIR", "")
SYNCED_DIR = os.path.join(_DATA_DIR, "synced")
TRASH_DIR = os.path.join(SYNCED_DIR, ".trash")
SYNC_METADATA_FILE = os.path.join(_DATA_DIR, "sync.json")   # 旧版元数据，首次启动时自动导入 sqlite
SYNC_DB_FILE = os.path.join(_DATA_DIR, "sync.db")
RETENTION_DAYS = 30

_sync_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None  # 单连接，所有访问都在 _sync_lock 下

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS folders (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, created INTEGER NOT NULL, next_rev INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS manifest (
    folder_id TEXT NOT NULL, path TEXT NOT NULL,
    size INTEGER, mtime INTEGER, sha256 TEXT,
    rev INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0, device TEXT,
    PRIMARY KEY (folder_id, path)
) WITHOUT ROWID;
"""


def ensure_dirs():
    """Create synced and trash directories if they don't exist."""
    os.makedirs(SYNCED_DIR, exist_ok=True)
    os.makedirs(TRASH_DIR, exist_ok=True)


def _db() -> sqlite3.Connection:
    """惰性打开数据库；首次创建时导入旧的 sync.json。调用方必须持有 _sync_lock。"""
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(os.path.abspath(SYNC_DB_FILE)), exist_ok=True)
        conn = sqlite3.connect(SYNC_DB_FILE, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        _conn = conn
        _migrate_legacy_json(conn)
    return _conn


def _migrate_legacy_json(conn: sqlite3.Connection):
    """旧版把元数据放在 sync.json：在一个事务里导入，成功后改名为 sync.json.migrated 留作备份。"""
    if not os.path.exists(SYNC_METADATA_FILE):
        return
    if conn.execute("SELECT 1 FROM folders LIMIT 1").fetchone() or conn.execute("SELECT 1 FROM meta LIMIT 1").fetchone():
        return  # 数据库里已有数据，不覆盖
    try:
        with open(SYNC_METADATA_FILE, "r") as f:
            data = json.load(f)
        folders = data["folders"]
    except (OSError, ValueError, KeyError, TypeError):
        return  # 旧文件损坏就保持原样，不动它
    conn.execute("BEGIN")
    try:
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('next_folder_id', ?)", (str(data.get("next_folder_id", 1)),))
        for fid, info in folders.items():
            conn.execute("INSERT INTO folders VALUES (?,?,?,?)",
                         (fid, info["name"], int(info.get("created", 0)), int(info.get("next_rev", 1))))
            conn.executemany(
                "INSERT INTO manifest VALUES (?,?,?,?,?,?,?,?)",
                [(fid, path, e.get("size"), e.get("mtime"), e.get("sha256"),
                  int(e.get("rev", 0)), 1 if e.get("deleted") else 0, e.get("device"))
                 for path, e in info.get("manifest", {}).items()],
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    try:
        os.replace(SYNC_METADATA_FILE, SYNC_METADATA_FILE + ".migrated")
    except OSError:
        pass


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


def _row_to_entry(size, mtime, sha256, rev, deleted, device) -> Dict[str, Any]:
    if deleted:
        return {"deleted": True, "rev": rev, "device": device, "mtime": mtime}
    return {"size": size, "mtime": mtime, "sha256": sha256, "rev": rev, "deleted": False, "device": device}


def create_folder(name: str) -> Tuple[Optional[str], str]:
    """
    Create a new sync folder.
    Returns (folder_id, error_message).
    """
    if not name or not isinstance(name, str) or len(name) > 255:
        return None, "Invalid folder name"
    with _sync_lock:
        ensure_dirs()
        db = _db()
        row = db.execute("SELECT value FROM meta WHERE key='next_folder_id'").fetchone()
        next_id = int(row[0]) if row else 1
        folder_id = f"folder_{next_id}"
        os.makedirs(os.path.join(SYNCED_DIR, folder_id), exist_ok=True)
        db.execute("BEGIN")
        try:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('next_folder_id', ?)", (str(next_id + 1),))
            db.execute("INSERT INTO folders VALUES (?,?,?,1)", (folder_id, name, int(time.time())))
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        return folder_id, ""


def delete_folder(folder_id: str) -> str:
    """
    Delete a sync folder (move to trash).
    Returns error message or "" on success.
    """
    with _sync_lock:
        db = _db()
        if not db.execute("SELECT 1 FROM folders WHERE id=?", (folder_id,)).fetchone():
            return "Folder not found"

        folder_path = os.path.join(SYNCED_DIR, folder_id)
        trash_path = os.path.join(TRASH_DIR, f"{folder_id}_{int(time.time())}")
        try:
            ensure_dirs()
            if os.path.exists(folder_path):
                os.rename(folder_path, trash_path)
            db.execute("BEGIN")
            try:
                db.execute("DELETE FROM manifest WHERE folder_id=?", (folder_id,))
                db.execute("DELETE FROM folders WHERE id=?", (folder_id,))
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
            return ""
        except (OSError, IOError, sqlite3.Error) as e:
            return f"Failed to delete folder: {e}"


def get_folders() -> Dict[str, Dict[str, Any]]:
    """Get list of all sync folders with stats."""
    with _sync_lock:
        rows = _db().execute(
            """SELECT f.id, f.name, f.created,
                      COALESCE(SUM(CASE WHEN m.deleted=0 THEN 1 ELSE 0 END), 0),
                      MAX(f.created, COALESCE(MAX(m.mtime), 0))
               FROM folders f LEFT JOIN manifest m ON m.folder_id = f.id
               GROUP BY f.id ORDER BY f.rowid"""
        ).fetchall()
        return {
            fid: {"name": name, "created": created, "file_count": count, "last_modified": last}
            for fid, name, created, count, last in rows
        }


def get_manifest(folder_id: str) -> Tuple[Optional[Dict], str]:
    """
    Get folder manifest (a snapshot, safe to serialize while others write).
    Returns (manifest_dict, error_message).
    Manifest format: {path: {size, mtime, sha256, rev, deleted, device}}
    """
    with _sync_lock:
        db = _db()
        if not db.execute("SELECT 1 FROM folders WHERE id=?", (folder_id,)).fetchone():
            return None, "Folder not found"
        rows = db.execute(
            "SELECT path, size, mtime, sha256, rev, deleted, device FROM manifest WHERE folder_id=?", (folder_id,)
        ).fetchall()
        return {path: _row_to_entry(*rest) for path, *rest in rows}, ""


def _current_rev(db: sqlite3.Connection, folder_id: str, rel_path: str) -> int:
    row = db.execute("SELECT rev FROM manifest WHERE folder_id=? AND path=?", (folder_id, rel_path)).fetchone()
    return row[0] if row else 0


def _commit_entry(db: sqlite3.Connection, folder_id: str, rel_path: str, size, mtime, sha256, deleted: bool, device: str) -> int:
    """在事务里分配新 rev 并写入 manifest 一行，返回新 rev。"""
    db.execute("BEGIN IMMEDIATE")
    try:
        new_rev = db.execute("SELECT next_rev FROM folders WHERE id=?", (folder_id,)).fetchone()[0]
        db.execute("UPDATE folders SET next_rev=? WHERE id=?", (new_rev + 1, folder_id))
        db.execute(
            "INSERT OR REPLACE INTO manifest VALUES (?,?,?,?,?,?,?,?)",
            (folder_id, rel_path, size, mtime, sha256, new_rev, 1 if deleted else 0, device),
        )
        db.execute("COMMIT")
        return new_rev
    except Exception:
        db.execute("ROLLBACK")
        raise


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

    慢的部分（接收请求体、算 sha256）都在锁外完成，锁里只做冲突检查和提交，
    一个慢客户端不会阻塞其他人同步。
    """
    # 先快速检查一遍：有冲突就不必浪费带宽去收文件
    with _sync_lock:
        ensure_dirs()
        db = _db()
        if not db.execute("SELECT 1 FROM folders WHERE id=?", (folder_id,)).fetchone():
            return False, "Folder not found", 0
        safe_path = safe_join(os.path.join(SYNCED_DIR, folder_id), rel_path)
        if not safe_path:
            return False, "Invalid path", 0
        current_rev = _current_rev(db, folder_id, rel_path)
        if base_rev is not None and base_rev != current_rev:
            return False, "Conflict: base revision mismatch", current_rev

    temp_path = None
    try:
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(safe_path), prefix=".sfd_upload_")
        digest = hashlib.sha256()
        bytes_written = 0
        with os.fdopen(temp_fd, "wb") as out:   # 带缓冲的文件对象，不会出现 os.write 短写
            while True:
                chunk = file_obj.read(65536)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                bytes_written += len(chunk)

        if expected_length is not None and bytes_written != expected_length:
            os.unlink(temp_path)
            return False, f"Upload incomplete: expected {expected_length} bytes, got {bytes_written}", 0
        os.chmod(temp_path, 0o644)

        with _sync_lock:
            db = _db()
            if not db.execute("SELECT 1 FROM folders WHERE id=?", (folder_id,)).fetchone():
                os.unlink(temp_path)
                return False, "Folder not found", 0
            current_rev = _current_rev(db, folder_id, rel_path)
            if base_rev is not None and base_rev != current_rev:
                os.unlink(temp_path)   # 接收期间别人已经提交了新版本
                return False, "Conflict: base revision mismatch", current_rev

            file_mtime = int(os.path.getmtime(temp_path))
            os.replace(temp_path, safe_path)
            temp_path = None
            new_rev = _commit_entry(db, folder_id, rel_path, bytes_written, file_mtime, digest.hexdigest(), False, device)
            return True, "", new_rev

    except (OSError, IOError, sqlite3.Error) as e:
        return False, f"Failed to write file: {e}", 0
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def read_file(folder_id: str, rel_path: str) -> Tuple[Optional[str], str]:
    """
    Get absolute path to a file in a sync folder (for reading).
    Returns (file_path, error_message).
    """
    with _sync_lock:
        known = _db().execute("SELECT 1 FROM folders WHERE id=?", (folder_id,)).fetchone()

    if not known:
        return None, "Folder not found"

    safe_path = safe_join(os.path.join(SYNCED_DIR, folder_id), rel_path)
    if not safe_path or not os.path.isfile(safe_path):
        return None, "File not found"

    return safe_path, ""


def delete_file(folder_id: str, rel_path: str, base_rev: Optional[int] = None, device: str = "host") -> Tuple[bool, str]:
    """
    Delete a file in a sync folder (mark as deleted in manifest).
    Returns (success, error_message).
    """
    with _sync_lock:
        db = _db()
        if not db.execute("SELECT 1 FROM folders WHERE id=?", (folder_id,)).fetchone():
            return False, "Folder not found"

        safe_path = safe_join(os.path.join(SYNCED_DIR, folder_id), rel_path)
        if not safe_path:
            return False, "Invalid path"

        if base_rev is not None and base_rev != _current_rev(db, folder_id, rel_path):
            return False, "Conflict: base revision mismatch"

        try:
            if os.path.exists(safe_path):
                os.unlink(safe_path)
            _commit_entry(db, folder_id, rel_path, None, int(time.time()), None, True, device)
            return True, ""
        except (OSError, IOError, sqlite3.Error) as e:
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
