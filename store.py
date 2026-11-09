"""数据层：文字/文件记录（内存缓存 + JSON 落盘）、在线状态、版本号长轮询、过期清理。"""

import datetime
import hashlib
import json
import os
import re
import threading
import time
import traceback
import uuid
from config import (
    ANIMAL_WORDS,
    AUDIO_EXTS,
    FILES_META,
    GHOST_RETENTION_DAYS,
    IMAGE_EXTS,
    LARGE_FILE_BYTES,
    ONLINE_WINDOW_SECONDS,
    PRESENCE_PRUNE_SECONDS,
    SHARE_DIR,
    SWEEP_INTERVAL_SECONDS,
    TEXTS_FILE,
    TTL_LARGE_DAYS,
    TTL_NORMAL_DAYS,
    VIDEO_EXTS
)


_texts_lock = threading.RLock()


_files_lock = threading.RLock()


_presence_lock = threading.Lock()


_version_cond = threading.Condition()


_presence = {}      # client_id -> last_seen (epoch seconds)


_state_version = [0]


_texts_cache = None   # 内存中的文字记录，None 表示尚未从磁盘加载


_files_cache = None   # 内存中的文件记录


_files_dirty = False  # 仅更新了 last_seen_ts 等低价值字段时，延迟到清理线程里落盘


def bump_version():
    """内容有变化：版本号 +1，并唤醒所有挂起的长轮询。"""
    with _version_cond:
        _state_version[0] += 1
        _version_cond.notify_all()


def current_version():
    with _version_cond:
        return _state_version[0]


def wait_version_change(since, timeout):
    """版本号还等于 since 时挂起，最多 timeout 秒；返回当前版本号。"""
    with _version_cond:
        _version_cond.wait_for(lambda: _state_version[0] != since, timeout)
        return _state_version[0]


def touch_presence(client_id):
    now = time.time()
    with _presence_lock:
        _presence[client_id] = now
        if len(_presence) > 200:
            cutoff = now - PRESENCE_PRUNE_SECONDS
            for k in [k for k, v in _presence.items() if v < cutoff]:
                del _presence[k]


def online_count():
    now = time.time()
    cutoff = now - ONLINE_WINDOW_SECONDS
    with _presence_lock:
        return sum(1 for v in _presence.values() if v >= cutoff)


def friendly_name_for(client_id):
    h = int(hashlib.md5(client_id.encode("utf-8")).hexdigest(), 16)  # 仅用于稳定地挑昵称，非安全用途
    return ANIMAL_WORDS[h % len(ANIMAL_WORDS)]


def kind_for_ext(ext):
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    return "file"


class HistoryReadError(RuntimeError):
    pass


def _read_json_list(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise HistoryReadError(os.path.basename(path)) from exc
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise HistoryReadError(os.path.basename(path))
    return data


def _write_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def load_texts():
    """返回文字记录的浅拷贝；记录字典与缓存共享，修改后必须调用 save_texts。"""
    global _texts_cache
    with _texts_lock:
        if _texts_cache is None:
            _texts_cache = _read_json_list(TEXTS_FILE)
        return list(_texts_cache)


def save_texts(items):
    global _texts_cache
    with _texts_lock:
        _write_json_atomic(TEXTS_FILE, items)
        _texts_cache = items


def add_text(content, sender, client_id, request_id=None):
    with _texts_lock:
        items = load_texts()
        if request_id:
            for item in items:
                if item.get("client_id") == client_id and item.get("request_id") == request_id:
                    return item["content"] == content
        items.append({
            "id": uuid.uuid4().hex,
            "content": content,
            "ts": datetime.datetime.now().timestamp(),
            "sender": sender,
            "client_id": client_id,
            "starred_by": [],
            **({"request_id": request_id} if request_id else {}),
        })
        save_texts(items)
    bump_version()
    return True


def delete_text(text_id, client_id, is_host):
    with _texts_lock:
        items = load_texts()
        target = None
        for it in items:
            if it.get("id") == text_id:
                target = it
                break
        if target is None:
            return "notfound"
        if not is_host and target.get("client_id") != client_id:
            return "forbidden"
        items = [it for it in items if it.get("id") != text_id]
        save_texts(items)
    bump_version()
    return "ok"


def clear_texts():
    with _texts_lock:
        load_texts()
        save_texts([])
    bump_version()


def toggle_star_text(text_id, client_id):
    with _texts_lock:
        items = load_texts()
        target = None
        for it in items:
            if it.get("id") == text_id:
                target = it
                break
        if target is None:
            return None
        starred = set(target.get("starred_by") or [])
        if client_id in starred:
            starred.discard(client_id)
            result = False
        else:
            starred.add(client_id)
            result = True
        target["starred_by"] = list(starred)
        save_texts(items)
    return result


def load_files():
    """返回文件记录的浅拷贝；记录字典与缓存共享，修改后必须调用 save_files。"""
    global _files_cache
    with _files_lock:
        if _files_cache is None:
            _files_cache = _read_json_list(FILES_META)
        return list(_files_cache)


def save_files(items):
    global _files_cache, _files_dirty
    with _files_lock:
        _write_json_atomic(FILES_META, items)
        _files_cache = items
        _files_dirty = False


def flush_files_if_dirty():
    with _files_lock:
        if _files_dirty and _files_cache is not None:
            save_files(list(_files_cache))


_FILENAME_BAD = re.compile(r"[\x00-\x1f\x7f]")


def clean_filename(raw):
    """把客户端给的文件名收敛成安全的单层文件名：去路径、去控制字符、去开头的点、限长。"""
    name = _FILENAME_BAD.sub("", raw or "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip().lstrip(".").strip()
    if not name or name in (".", ".."):
        return "file"
    if len(name.encode("utf-8")) > 200:
        base, ext = os.path.splitext(name)
        ext = ext[:20]
        while len((base + ext).encode("utf-8")) > 200:
            base = base[:-1]
        name = (base or "file") + ext
    return name


def safe_share_path(name):
    """把 URL 里的名字映射到 SHARE_DIR 内的真实路径；不合法或越界返回 None。"""
    if not name or name.startswith(".") or "/" in name or "\\" in name or "\x00" in name:
        return None
    root = os.path.realpath(SHARE_DIR)
    path = os.path.realpath(os.path.join(root, name))
    if os.path.dirname(path) != root:
        return None
    return path


def _dedup_path(name, items):
    """调用方必须持有 _files_lock，选名和落盘才是原子的。"""
    dest = os.path.join(SHARE_DIR, name)
    base, ext = os.path.splitext(name)
    i = 1
    reserved = {it["name"] for it in items if not it.get("purged") or it.get("purge_pending")}
    while os.path.lexists(dest) or os.path.basename(dest) in reserved:
        dest = os.path.join(SHARE_DIR, f"{base}({i}){ext}")
        i += 1
    return dest


def add_file(original_filename, tmp_path, size, sender, client_id, request_id=None):
    """把已写好的临时文件落成共享文件并登记。tmp_path 必须在 SHARE_DIR 同一文件系统内。"""
    safe = clean_filename(original_filename)
    now = datetime.datetime.now().timestamp()
    with _files_lock:
        items = load_files()
        digest = None
        if request_id:
            hasher = hashlib.sha256()
            with open(tmp_path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(chunk)
            digest = hasher.hexdigest()
            for item in items:
                if item.get("client_id") == client_id and item.get("request_id") == request_id:
                    if item.get("upload_sha256") != digest or item.get("original_filename") != original_filename:
                        raise ValueError("发送请求编号已用于其他文件")
                    os.remove(tmp_path)
                    return item
        dest = _dedup_path(safe, items)
        os.replace(tmp_path, dest)
        final_name = os.path.basename(dest)
        ext = os.path.splitext(final_name)[1].lower().lstrip(".")
        entry = {
            "id": uuid.uuid4().hex,
            "name": final_name,
            "kind": kind_for_ext(ext),
            "size": size,
            "uploaded_ts": now,
            "last_seen_ts": now,
            "sender": sender,
            "client_id": client_id,
            "purged": False,
            "purged_ts": None,
            "starred_by": [],
            **({"request_id": request_id, "upload_sha256": digest, "original_filename": original_filename} if request_id else {}),
        }
        items.append(entry)
        try:
            save_files(items)
        except OSError:
            try:
                os.remove(dest)  # 登记失败就不留孤儿文件
            except OSError:
                pass
            raise
    bump_version()
    return entry


def open_shared_file(name, touch=False):
    """在锁内检查记录并打开文件，避免清理与下载打开不同版本。"""
    global _files_dirty
    with _files_lock:
        for it in load_files():
            if it.get("name") != name or it.get("purged"):
                continue
            now = datetime.datetime.now().timestamp()
            if now - it.get("uploaded_ts", now) >= ttl_days_for_size(it.get("size", 0)) * 86400:
                return None
            path = safe_share_path(name)
            if not path or not os.path.isfile(path):
                return None
            try:
                stream = open(path, "rb")
            except FileNotFoundError:
                return None
            if touch:
                it["last_seen_ts"] = now
                _files_dirty = True
            return stream
    return None


def _remove_shared_file(name):
    if not safe_share_path(name):
        raise OSError("Invalid shared file path")
    try:
        os.remove(os.path.join(SHARE_DIR, name))
    except FileNotFoundError:
        pass


def delete_file(entry_id, client_id, is_host):
    with _files_lock:
        items = load_files()
        target = None
        for it in items:
            if it.get("id") == entry_id:
                target = it
                break
        if target is None:
            return "notfound"
        if not is_host and target.get("client_id") != client_id:
            return "forbidden"
        save_files([it for it in items if it is not target])
        if not target.get("purged") or target.get("purge_pending"):
            try:
                _remove_shared_file(target["name"])
            except OSError:
                save_files(items)
                raise
    bump_version()
    return "ok"


def reshare_file(entry_id, tmp_path, size, sender, client_id):
    """成功返回记录；记录不存在或未过期返回 None（临时文件由调用方清理）。"""
    with _files_lock:
        items = load_files()
        target = None
        for index, it in enumerate(items):
            if it.get("id") == entry_id:
                target = it
                break
        if target is None or not target.get("purged"):
            return None
        if target.get("purge_pending"):
            _remove_shared_file(target["name"])
        dest = _dedup_path(target["name"], (it for it in items if it is not target))
        os.replace(tmp_path, dest)
        final_name = os.path.basename(dest)
        now = datetime.datetime.now().timestamp()
        restored = {
            **target,
            "name": final_name,
            "size": size,
            "uploaded_ts": now,
            "last_seen_ts": now,
            "sender": sender,
            "client_id": client_id,
            "purged": False,
            "purged_ts": None,
        }
        restored.pop("purge_pending", None)
        items[index] = restored
        try:
            save_files(items)
        except OSError:
            os.replace(dest, tmp_path)
            raise
    bump_version()
    return restored


def toggle_star_file(entry_id, client_id):
    with _files_lock:
        items = load_files()
        target = None
        for it in items:
            if it.get("id") == entry_id:
                target = it
                break
        if target is None:
            return None
        starred = set(target.get("starred_by") or [])
        if client_id in starred:
            starred.discard(client_id)
            result = False
        else:
            starred.add(client_id)
            result = True
        target["starred_by"] = list(starred)
        save_files(items)
    return result


def ttl_days_for_size(size):
    return TTL_LARGE_DAYS if size >= LARGE_FILE_BYTES else TTL_NORMAL_DAYS


def sweep_once():
    now = datetime.datetime.now().timestamp()
    changed = False
    with _files_lock:
        items = [dict(it) for it in load_files()]
        keep = []
        for it in items:
            if not it.get("purged") and now - it.get("uploaded_ts", now) >= ttl_days_for_size(it.get("size", 0)) * 86400:
                it.update(purged=True, purged_ts=now, purge_pending=True)
                changed = True
            if it.get("purge_pending"):
                try:
                    _remove_shared_file(it["name"])
                except OSError:
                    keep.append(it)
                    continue
                it.pop("purge_pending")
                changed = True
            if it.get("purged") and now - (it.get("purged_ts") or now) >= GHOST_RETENTION_DAYS * 86400:
                changed = True
                continue
            keep.append(it)
        if changed:
            save_files(keep)
        else:
            flush_files_if_dirty()
    if changed:
        bump_version()


def cleanup_stale_uploads():
    """清掉上次异常退出遗留的上传临时文件。"""
    try:
        for entry in os.scandir(SHARE_DIR):
            if entry.name.startswith(".upload-") and entry.name.endswith(".part"):
                try:
                    os.remove(entry.path)
                except OSError:
                    pass
    except OSError:
        pass


def sweep_loop():
    while True:
        try:
            sweep_once()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        time.sleep(SWEEP_INTERVAL_SECONDS)
