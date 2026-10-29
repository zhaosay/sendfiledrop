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


def _read_json_list(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


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


def add_text(content, sender, client_id):
    with _texts_lock:
        items = load_texts()
        items.append({
            "id": uuid.uuid4().hex,
            "content": content,
            "ts": datetime.datetime.now().timestamp(),
            "sender": sender,
            "client_id": client_id,
            "starred_by": [],
        })
        save_texts(items)
    bump_version()


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


def _dedup_path(name):
    """调用方必须持有 _files_lock，选名和落盘才是原子的。"""
    dest = os.path.join(SHARE_DIR, name)
    base, ext = os.path.splitext(name)
    i = 1
    while os.path.exists(dest):
        dest = os.path.join(SHARE_DIR, f"{base}({i}){ext}")
        i += 1
    return dest


def add_file(original_filename, tmp_path, size, sender, client_id):
    """把已写好的临时文件落成共享文件并登记。tmp_path 必须在 SHARE_DIR 同一文件系统内。"""
    safe = clean_filename(original_filename)
    now = datetime.datetime.now().timestamp()
    with _files_lock:
        dest = _dedup_path(safe)
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
        }
        items = load_files()
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


def touch_file_access(name):
    """只改内存里的 last_seen_ts，落盘交给清理线程批量做，避免每次下载/播放都重写 files.json。"""
    global _files_dirty
    with _files_lock:
        for it in load_files():
            if it.get("name") == name and not it.get("purged"):
                it["last_seen_ts"] = datetime.datetime.now().timestamp()
                _files_dirty = True
                break


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
        fpath = None if target.get("purged") else os.path.join(SHARE_DIR, target["name"])
        items = [it for it in items if it.get("id") != entry_id]
        save_files(items)
    if fpath:
        try:
            if os.path.isfile(fpath):
                os.remove(fpath)
        except OSError:
            pass
    bump_version()
    return "ok"


def reshare_file(entry_id, tmp_path, size, sender, client_id):
    """成功返回记录；记录不存在或未过期返回 None（临时文件由调用方清理）。"""
    with _files_lock:
        items = load_files()
        target = None
        for it in items:
            if it.get("id") == entry_id:
                target = it
                break
        if target is None or not target.get("purged"):
            return None
        dest = _dedup_path(target["name"])
        os.replace(tmp_path, dest)
        final_name = os.path.basename(dest)
        now = datetime.datetime.now().timestamp()
        target.update({
            "name": final_name,
            "size": size,
            "uploaded_ts": now,
            "last_seen_ts": now,
            "sender": sender,
            "client_id": client_id,
            "purged": False,
            "purged_ts": None,
        })
        save_files(items)
    bump_version()
    return target


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
    to_remove = []
    with _files_lock:
        items = load_files()
        keep = []
        for it in items:
            if not it.get("purged"):
                ttl_days = ttl_days_for_size(it.get("size", 0))
                if now - it.get("uploaded_ts", now) >= ttl_days * 86400:
                    to_remove.append(os.path.join(SHARE_DIR, it.get("name", "")))
                    it["purged"] = True
                    it["purged_ts"] = now
                    changed = True
                keep.append(it)
            else:
                if now - (it.get("purged_ts") or now) >= GHOST_RETENTION_DAYS * 86400:
                    changed = True
                    continue
                keep.append(it)
        if changed:
            save_files(keep)
        else:
            flush_files_if_dirty()
    # 删大文件可能很慢，放到锁外做，避免阻塞上传和列表
    for fpath in to_remove:
        try:
            if os.path.isfile(fpath):
                os.remove(fpath)
        except OSError:
            pass
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
