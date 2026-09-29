#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sendfiledrop 局域网文件传输服务器 (零依赖，仅用 Python 标准库)

用法:
    python3 server.py            # 默认端口 8000
    python3 server.py 9000       # 指定端口

启动后，同一 WiFi / 局域网下的任何设备(手机、平板、别的电脑)
用浏览器打开显示的地址即可上传/下载文件、发送文字，界面是统一的聊天时间流。

上传的文件保存在本脚本同目录下的 "shared" 文件夹里；
文件元数据保存在 "files.json"，文字消息保存在 "texts.json"。

文件生命周期:
    - 小于 1GB 的文件，上传满 3 天自动清除内容(只删内容，记录还在，可"重新分享")
    - 大于等于 1GB 的文件，上传满 1 天自动清除内容
    - 记录本身在内容清除后超过 30 天没人重新分享，会被彻底移除
"""

import os
import sys
import json
import html
import socket
import urllib.parse
import http.server
import socketserver
import datetime
import mimetypes
import threading
import uuid
import time
import hashlib
import io

# ---- 导入同步模块 ----
try:
    import sync_store
except ImportError:
    sync_store = None

# ---- 配置 ----
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SHARE_DIR = os.path.join(BASE_DIR, "shared")
TEXTS_FILE = os.path.join(BASE_DIR, "texts.json")
FILES_META = os.path.join(BASE_DIR, "files.json")
os.makedirs(SHARE_DIR, exist_ok=True)

IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "ico", "heic", "heif"}
VIDEO_EXTS = {"mp4", "mov", "m4v", "avi", "mkv", "webm", "3gp", "wmv", "flv"}
AUDIO_EXTS = {"mp3", "wav", "m4a", "aac", "flac", "ogg", "wma"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS

LARGE_FILE_BYTES = 1024 ** 3       # 1GB：达到这个体积走"1天"过期
TTL_NORMAL_DAYS = 3                # 普通文件：上传满 3 天自动清除内容
TTL_LARGE_DAYS = 1                 # 大文件：上传满 1 天自动清除内容
GHOST_RETENTION_DAYS = 30          # 内容清除后，记录本身再保留 30 天没人重新分享就彻底删除
SWEEP_INTERVAL_SECONDS = 60        # 后台清理线程扫描间隔
ONLINE_WINDOW_SECONDS = 12         # 心跳窗口：这段时间内有请求就算"在线"
PRESENCE_PRUNE_SECONDS = 300       # 在线心跳记录的兜底清理窗口

ANIMAL_WORDS = [
    "浣熊", "水獭", "柯基", "熊猫", "狐狸", "考拉", "刺猬", "企鹅", "松鼠", "河马",
    "长颈鹿", "羊驼", "仓鼠", "海豚", "猫头鹰", "变色龙", "树懒", "犀牛", "斑马", "章鱼",
    "水母", "雪貂", "土拨鼠", "北极熊", "袋鼠", "蜂鸟", "小熊猫", "锦鲤", "海獭", "猎豹",
]

_texts_lock = threading.Lock()
_files_lock = threading.Lock()
_presence_lock = threading.Lock()
_version_lock = threading.Lock()
_presence = {}      # client_id -> last_seen (epoch seconds)
_version = [0]

# 离线二维码生成器 (同目录下的 qr.py，纯标准库，无需联网)
sys.path.insert(0, BASE_DIR)
try:
    import qr as _qr
except Exception:  # noqa: BLE001
    _qr = None


def qr_svg_for(url):
    """把 URL 生成二维码 SVG；若二维码模块缺失则返回空串。"""
    if _qr is None:
        return ""
    try:
        return _qr.matrix_to_svg(_qr.generate_matrix(url), box=4, border=2)
    except Exception:  # noqa: BLE001
        return ""


def human_size(n):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def format_remaining(seconds):
    if seconds <= 0:
        return "即将清除"
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    if days >= 1:
        return f"{days}天{hours}小时后清除"
    if hours >= 1:
        return f"{hours}小时{minutes}分钟后清除"
    if minutes >= 1:
        return f"{minutes}分钟后清除"
    return "即将清除"


def get_lan_ip():
    """获取本机在局域网中的 IP 地址"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def _dir_size(path):
    total = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def bump_version():
    with _version_lock:
        _version[0] += 1


def current_version():
    with _version_lock:
        return _version[0]


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
    h = int(hashlib.md5(client_id.encode("utf-8")).hexdigest(), 16)
    return ANIMAL_WORDS[h % len(ANIMAL_WORDS)]


def kind_for_ext(ext):
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    return "file"


# ---------------------------------------------------------------------------
# 文字消息存储
# ---------------------------------------------------------------------------

def load_texts():
    try:
        with open(TEXTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_texts(items):
    tmp = TEXTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TEXTS_FILE)


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


# ---------------------------------------------------------------------------
# 文件元数据存储
# ---------------------------------------------------------------------------

def load_version():
    try:
        with open(os.path.join(BASE_DIR, "version.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("version", "unknown")
    except (OSError, ValueError):
        return "unknown"


def load_files():
    try:
        with open(FILES_META, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_files(items):
    tmp = FILES_META + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, FILES_META)


def _dedup_path(name):
    dest = os.path.join(SHARE_DIR, name)
    base, ext = os.path.splitext(name)
    i = 1
    while os.path.exists(dest):
        dest = os.path.join(SHARE_DIR, f"{base}({i}){ext}")
        i += 1
    return dest


def add_file(original_filename, content, sender, client_id):
    safe = os.path.basename(original_filename) or "file"
    dest = _dedup_path(safe)
    final_name = os.path.basename(dest)
    with open(dest, "wb") as out:
        out.write(content)
    ext = os.path.splitext(final_name)[1].lower().lstrip(".")
    now = datetime.datetime.now().timestamp()
    entry = {
        "id": uuid.uuid4().hex,
        "name": final_name,
        "kind": kind_for_ext(ext),
        "size": len(content),
        "uploaded_ts": now,
        "last_seen_ts": now,
        "sender": sender,
        "client_id": client_id,
        "purged": False,
        "purged_ts": None,
        "starred_by": [],
    }
    with _files_lock:
        items = load_files()
        items.append(entry)
        save_files(items)
    bump_version()
    return entry


def touch_file_access(name):
    with _files_lock:
        items = load_files()
        changed = False
        for it in items:
            if it.get("name") == name and not it.get("purged"):
                it["last_seen_ts"] = datetime.datetime.now().timestamp()
                changed = True
                break
        if changed:
            save_files(items)


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
        if not target.get("purged"):
            fpath = os.path.join(SHARE_DIR, target["name"])
            try:
                if os.path.isfile(fpath):
                    os.remove(fpath)
            except OSError:
                pass
        items = [it for it in items if it.get("id") != entry_id]
        save_files(items)
    bump_version()
    return "ok"


def reshare_file(entry_id, content, sender, client_id):
    with _files_lock:
        items = load_files()
        target = None
        for it in items:
            if it.get("id") == entry_id:
                target = it
                break
        if target is None or not target.get("purged"):
            return None
        dest = os.path.join(SHARE_DIR, target["name"])
        if os.path.exists(dest):
            dest = _dedup_path(target["name"])
        with open(dest, "wb") as out:
            out.write(content)
        final_name = os.path.basename(dest)
        now = datetime.datetime.now().timestamp()
        target.update({
            "name": final_name,
            "size": len(content),
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
    with _files_lock:
        items = load_files()
        keep = []
        for it in items:
            if not it.get("purged"):
                ttl_days = ttl_days_for_size(it.get("size", 0))
                if now - it.get("uploaded_ts", now) >= ttl_days * 86400:
                    fpath = os.path.join(SHARE_DIR, it.get("name", ""))
                    try:
                        if os.path.isfile(fpath):
                            os.remove(fpath)
                    except OSError:
                        pass
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
    if changed:
        bump_version()


def sweep_loop():
    while True:
        try:
            sweep_once()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(SWEEP_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# 渲染：把文字 / 文件元数据变成 <li> 列表项
# ---------------------------------------------------------------------------

def _empty_block(title, sub):
    return (
        '<div class="empty"><div><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">'
        '<path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/>'
        f'</svg><br>{title}<br><span style="font-size:11px">{sub}</span></div></div>'
    )


def _kind_icon(kind, ext_label=""):
    if kind == "file":
        return (
            f'<div class="file-icon" title="{ext_label} 文件">'
            f'<span style="font-size:10px;font-weight:800;letter-spacing:-.03em">{ext_label}</span></div>'
        )
    bodies = {
        "image": '<path d="M4 5h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Z"/><circle cx="9" cy="10" r="1.4"/><path d="m4 17 5-5 3 3 4-4 4 4"/>',
        "video": '<rect x="3" y="6" width="14" height="12" rx="2"/><path d="m21 8-4 3 4 3V8Z"/>',
        "audio": '<path d="M9 18V5l10-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="16" cy="16" r="3"/>',
    }
    body = bodies.get(kind, "")
    return (
        f'<div class="file-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        f'stroke-width="1.8">{body}</svg></div>'
    )


def _star_btn(kind, item_id, is_starred):
    cls = "dl star-btn starred" if is_starred else "dl star-btn"
    return (
        f'<button class="{cls}" data-kind="{kind}" data-key="{html.escape(item_id)}" title="收藏">'
        '<svg viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">'
        '<path d="M12 3.5l2.6 5.6 6.1.6-4.6 4.1 1.3 6-5.4-3.2-5.4 3.2 1.3-6-4.6-4.1 6.1-.6z" stroke-linejoin="round"/>'
        '</svg></button>'
    )


def render_text_item(entry, viewer_cid, is_host):
    content = entry.get("content", "")
    safe_content = html.escape(content)
    ts = entry.get("ts", 0)
    when = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    tid = html.escape(str(entry.get("id", "")))
    sender = html.escape(entry.get("sender") or "访客")
    is_mine = entry.get("client_id") == viewer_cid
    can_delete = is_host or is_mine
    side = "mine" if is_mine else "other"
    search_val = html.escape(content.lower())[:500]
    star_btn = _star_btn("text", str(entry.get("id", "")), viewer_cid in (entry.get("starred_by") or []))

    del_btn = ""
    if can_delete:
        del_btn = (
            f'<button class="del" data-kind="text" data-key="{tid}" title="删除这条文字" aria-label="删除这条文字">'
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
            '<path d="M4 7h16M9 7V4h6v3m3 0-1 13H7L6 7m4 4v5m4-5v5" stroke-linecap="round" stroke-linejoin="round"/>'
            '</svg></button>'
        )
    copy_btn = (
        f'<button class="dl copy-text" data-content="{safe_content}" title="复制文字" aria-label="复制文字">'
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
        '<rect x="9" y="9" width="11" height="11" rx="2"/>'
        '<path d="M15 9V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h3"/></svg></button>'
    )
    bubble = f'<div class="bubble"><div class="tcontent">{safe_content}</div></div>'
    return (
        f'<li class="msg {side}" data-search="{search_val}" data-meta="{ts}">'
        f'<div class="msg-meta">{sender} · {when}</div>'
        f'<div class="msg-row">{bubble}</div>'
        f'<div class="actions">{star_btn}{copy_btn}{del_btn}</div>'
        '</li>'
    )


def render_file_item(entry, viewer_cid, is_host):
    name = entry.get("name", "")
    safe_name = html.escape(name)
    eid = html.escape(entry.get("id", ""))
    kind = entry.get("kind", "file")
    sender = html.escape(entry.get("sender") or "访客")
    uploaded_ts = entry.get("uploaded_ts", 0)
    when = datetime.datetime.fromtimestamp(uploaded_ts).strftime("%Y-%m-%d %H:%M")
    purged = bool(entry.get("purged"))
    is_mine = entry.get("client_id") == viewer_cid
    can_delete = is_host or is_mine
    side = "mine" if is_mine else "other"
    ext = os.path.splitext(name)[1].lower().lstrip(".") or "file"
    ext_label = html.escape(ext[:5].upper())
    search_val = html.escape(name.lower())
    star_btn = _star_btn("file", str(entry.get("id", "")), viewer_cid in (entry.get("starred_by") or []))

    del_btn = ""
    if can_delete:
        del_btn = (
            f'<button class="del" data-kind="file" data-key="{eid}" title="删除 {safe_name}" aria-label="删除 {safe_name}">'
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
            '<path d="M4 7h16M9 7V4h6v3m3 0-1 13H7L6 7m4 4v5m4-5v5" stroke-linecap="round" stroke-linejoin="round"/>'
            '</svg></button>'
        )

    if purged:
        icon_html = _kind_icon(kind if kind == "file" else kind, ext_label)
        reshare_btn = (
            f'<button class="dl reshare-btn" data-key="{eid}" title="重新分享" aria-label="重新分享">'
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
            '<path d="M4 4v6h6M20 20v-6h-6M5 15a7 7 0 0 0 12.5 3.5M19 9A7 7 0 0 0 6.5 5.5" '
            'stroke-linecap="round" stroke-linejoin="round"/></svg></button>'
        )
        meta_line = '<span style="color:var(--danger)">已过期，内容已清除</span>'
        bubble = (
            f'<div class="bubble file-bubble"><div class="file-bubble-top">{icon_html}'
            f'<div class="fmeta"><div class="fname">{safe_name}</div>'
            f'<div class="meta">{human_size(entry.get("size", 0))} · {meta_line}</div></div></div></div>'
        )
        return (
            f'<li class="msg {side} ghost" data-search="{search_val}" data-meta="{uploaded_ts}">'
            f'<div class="msg-meta">{sender} · {when}</div>'
            f'<div class="msg-row">{bubble}</div>'
            f'<div class="actions">{star_btn}{reshare_btn}{del_btn}</div>'
            '</li>'
        )

    link = "/download/" + urllib.parse.quote(name)
    view_link = "/view/" + urllib.parse.quote(name)
    icon_html = _kind_icon("file", ext_label)
    preview_html = ""
    if kind == "image":
        icon_html = f'<div class="media-thumb"><img src="{view_link}" alt="{safe_name}" loading="lazy"></div>'
    elif kind == "video":
        preview_html = f'<div class="media-preview"><video controls preload="none" src="{view_link}"></video></div>'
    elif kind == "audio":
        preview_html = f'<div class="media-preview"><audio controls preload="none" src="{view_link}"></audio></div>'

    dl_btn = (
        f'<a class="dl" href="{link}" download="{safe_name}" title="下载 {safe_name}" aria-label="下载 {safe_name}">'
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
        '<path d="M12 4v11m0 0 4-4m-4 4-4-4M5 20h14" stroke-linecap="round" stroke-linejoin="round"/></svg></a>'
    )
    size_bytes = entry.get("size", 0)
    expire_ts = uploaded_ts + ttl_days_for_size(size_bytes) * 86400
    remaining = expire_ts - datetime.datetime.now().timestamp()
    expiry_cls = "expiry soon" if 0 < remaining < 3600 else "expiry"
    expiry_html = f'<span class="{expiry_cls}" data-expire="{expire_ts}">{format_remaining(remaining)}</span>'
    bubble_top = (
        f'<div class="file-bubble-top">{icon_html}<div class="fmeta"><div class="fname">{safe_name}</div>'
        f'<div class="meta">{human_size(size_bytes)} · {expiry_html}</div></div></div>'
    )
    bubble = f'<div class="bubble file-bubble">{bubble_top}{preview_html}</div>'
    return (
        f'<li class="msg {side}" data-search="{search_val}" data-meta="{uploaded_ts}">'
        f'<div class="msg-meta">{sender} · {when}</div>'
        f'<div class="msg-row">{bubble}</div>'
        f'<div class="actions">{star_btn}{dl_btn}{del_btn}</div>'
        '</li>'
    )


def build_feed(filter_key, viewer_cid, is_host):
    texts = load_texts()
    files = load_files()
    entries = [("text", t) for t in texts]
    for f in files:
        cat = "media" if f.get("kind") in ("image", "video", "audio") else "file"
        entries.append((cat, f))

    if filter_key != "all":
        entries = [e for e in entries if e[0] == filter_key]

    if filter_key in ("media", "file"):
        # 文件/多媒体是"浏览列表"：最近被碰过的排前面，长期没用的下沉到底部
        entries.sort(key=lambda e: e[1].get("last_seen_ts", e[1].get("uploaded_ts", 0)), reverse=True)
    else:
        # 全部/文字是"聊天时间流"：旧的在上，新的在下，跟聊天软件一致
        entries.sort(key=lambda e: e[1].get("ts", e[1].get("uploaded_ts", 0)))

    rows = []
    for cat, entry in entries:
        if cat == "text":
            rows.append(render_text_item(entry, viewer_cid, is_host))
        else:
            rows.append(render_file_item(entry, viewer_cid, is_host))

    if not rows:
        labels = {
            "all": ("暂无共享内容", "发送文字或上传文件后会显示在这里"),
            "text": ("暂无文字消息", "发送后会显示在这里"),
            "media": ("暂无多媒体文件", "上传图片 / 音频 / 视频后会显示在这里"),
            "file": ("暂无共享文件", "上传后会显示在这里"),
        }
        title, sub = labels.get(filter_key, labels["all"])
        return _empty_block(title, sub)
    return "<ul>" + "".join(rows) + "</ul>"


def get_active_senders():
    """获取所有活跃的发送者及其最后活动时间"""
    texts = load_texts()
    files = load_files()
    senders = {}
    for t in texts:
        sender = t.get("sender") or "访客"
        ts = t.get("ts", 0)
        if sender not in senders or ts > senders[sender]:
            senders[sender] = ts
    for f in files:
        sender = f.get("sender") or "访客"
        ts = f.get("last_seen_ts", f.get("uploaded_ts", 0))
        if sender not in senders or ts > senders[sender]:
            senders[sender] = ts
    sorted_senders = sorted(senders.items(), key=lambda x: x[1], reverse=True)
    return [{"name": name, "ts": ts} for name, ts in sorted_senders]


def build_favorites(viewer_cid, is_host):
    texts = load_texts()
    files = load_files()
    entries = [("text", t) for t in texts if viewer_cid in (t.get("starred_by") or [])]
    entries += [("file", f) for f in files if viewer_cid in (f.get("starred_by") or [])]
    entries.sort(key=lambda e: e[1].get("ts", e[1].get("uploaded_ts", 0)), reverse=True)

    rows = []
    for cat, entry in entries:
        if cat == "text":
            rows.append(render_text_item(entry, viewer_cid, is_host))
        else:
            rows.append(render_file_item(entry, viewer_cid, is_host))

    if not rows:
        return '<div class="fav-empty">暂无收藏<br><span>点内容操作栏里的收藏按钮即可收藏</span></div>'
    return "<ul>" + "".join(rows) + "</ul>"


# ---------------------------------------------------------------------------
# 页面模板 (占位符用 __XXX__ 做字符串替换，避免和 CSS/JS 里的花括号冲突)
# ---------------------------------------------------------------------------

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>sendfiledrop · 局域网文件传输</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&display=swap');
  :root {
    --ink:#172033; --muted:#6f7b91; --line:#e6eaf0; --blue:#2667ff; --blue2:#5b8cff; --green:#14b87a; --danger:#e5484d; --bg:#f4f7fb;
    --radius-sharp:0px; --radius-soft:10px; --radius-pill:999px;
    --text-primary:#172033;
    --text-secondary:#6f7b91;
    --text-tertiary:#9ca7b8;
    --text-interactive:#2667ff;
    --text-disabled:#c5cad1;
    --text-inverse:#ffffff;
    --text-success:#047857;
    --text-warning:#d97706;
    --text-error:#dc2626;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --text-primary:#f0f4f8;
      --text-secondary:#a8b2c1;
      --text-tertiary:#7a8494;
      --text-interactive:#5b8cff;
      --text-disabled:#5a6370;
      --text-inverse:#0f172a;
    }
  }
  * { box-sizing:border-box; }
  html,body { height:100%; margin:0; overflow:hidden; width:100%; }
  body { color:var(--text-primary); font-family:'Geist',-apple-system,BlinkMacSystemFont,"SF Pro Display","PingFang SC","Microsoft YaHei",sans-serif;
    -webkit-font-smoothing:antialiased; background:var(--bg); overscroll-behavior:none; }
  button,a { -webkit-tap-highlight-color:transparent; }
  button { font:inherit; }
  .app { display:flex; flex-direction:row; max-width:1120px; margin:0 auto; background:#fff;
    box-shadow:0 0 40px rgba(35,52,79,.06); overflow:hidden; position:relative;
    height:100vh; height:-webkit-fill-available; height:calc(var(--vh, 1vh) * 100); height:100dvh; }
  .sidebar { flex:0 0 250px; display:flex; flex-direction:column; gap:14px; padding:16px; overflow-y:auto; overflow-x:hidden;
    border-right:1px solid var(--line); background:#fbfcfe; }
  .main { flex:1; min-width:0; display:flex; flex-direction:column; position:relative; }
  .brand { display:flex; align-items:center; gap:10px; font-size:16px; font-weight:700; letter-spacing:-.02em; }
  .logo { display:grid; place-items:center; width:32px; height:32px; color:#fff; border-radius:var(--radius-soft);
    background:linear-gradient(145deg,var(--blue2),var(--blue)); box-shadow:0 6px 16px rgba(38,103,255,.25); }
  .logo svg { width:18px; }
  .online-pill { display:flex; align-items:center; gap:7px; color:var(--text-secondary); font-size:12px; font-weight:650;
    padding:6px 11px; border:1px solid var(--line); border-radius:var(--radius-pill); background:#fff; align-self:flex-start; }
  .dot { width:7px; height:7px; border-radius:50%; background:var(--green); box-shadow:0 0 0 3px rgba(20,184,122,.14); }
  .connect-block { display:flex; flex-direction:column; align-items:center; text-align:center; gap:9px; padding:14px 10px;
    border:1px solid var(--line); border-radius:var(--radius-soft); background:#fff; font-size:12px; }
  .qr-big svg { width:150px; height:150px; border-radius:var(--radius-soft); display:block; }
  .connect-info { min-width:0; width:100%; display:flex; flex-direction:column; align-items:center; gap:6px; }
  #urlText { color:var(--text-interactive); font-weight:650; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:100%; }
  .mini-btn { padding:5px 12px; color:var(--text-secondary); font-size:11px; font-weight:600; background:#f0f3f8; border:1px solid var(--line); border-radius:var(--radius-soft); cursor:pointer; transition:all .2s cubic-bezier(0.16,1,0.3,1); }
  .mini-btn:hover { color:var(--text-primary); background:#e8ecf2; border-color:#d1d5dd; }
  .nickname-pill { padding:8px 12px; color:#fff; font-size:12px; font-weight:650; border:0; border-radius:var(--radius-pill);
    background:linear-gradient(135deg,var(--blue2),var(--blue)); cursor:pointer; text-align:left; }
  .search-input { padding:8px 12px; font-size:12px; border:1px solid var(--line); border-radius:var(--radius-soft);
    background:#fff; color:var(--text-primary); width:100%; }
  .search-input::placeholder { color:var(--text-tertiary); }
  .search-input:focus { outline:none; border-color:var(--blue2); }
  .filter-bar { flex:none; display:flex; gap:7px; flex-wrap:wrap; padding:14px 20px 0; align-items:center; }
  .tab,.time-tab { padding:7px 14px; color:var(--text-secondary); font-size:12.5px; font-weight:650; border:1px solid var(--line);
    background:#fff; border-radius:var(--radius-pill); cursor:pointer; transition:all .2s cubic-bezier(0.16,1,0.3,1); }
  .tab:hover,.time-tab:hover { color:var(--text-interactive); border-color:var(--blue2); background:#f8faff; transform:translateY(-1px); }
  .tab:active,.time-tab:active { transform:translateY(0); }
  .tab.active,.time-tab.active { color:var(--text-inverse); background:linear-gradient(135deg,var(--blue2),var(--blue)); border-color:transparent; box-shadow:0 4px 12px rgba(38,103,255,.25); }
  .time-filter-sep { width:1px; height:20px; background:var(--line); margin:0 4px; }
  .senders-block { display:flex; flex-direction:column; gap:8px; }
  .senders-title { color:var(--text-secondary); font-size:10.5px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; margin-bottom:2px; }
  .senders-list { display:flex; flex-wrap:wrap; gap:6px; }
  .sender-btn { padding:6px 11px; font-size:11px; font-weight:500; color:var(--text-secondary); background:#fff; border:1px solid var(--line); border-radius:var(--radius-pill); cursor:pointer; transition:all .2s cubic-bezier(0.16,1,0.3,1); }
  .sender-btn:hover { color:var(--text-interactive); border-color:var(--blue2); background:#f8faff; transform:translateY(-1px); }
  .sender-btn:active { transform:translateY(0); }
  .sender-btn.active { color:var(--text-inverse); background:linear-gradient(135deg,var(--blue2),var(--blue)); border-color:transparent; box-shadow:0 4px 12px rgba(38,103,255,.25); }
  .favorites-block { flex:1; min-height:100px; display:flex; flex-direction:column; gap:8px; }
  .favorites-title { color:var(--text-secondary); font-size:10.5px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; margin-bottom:2px; }
  .favorites-list { flex:1; overflow-y:auto; }
  .favorites-list li { padding:9px 2px; }
  .fav-empty { color:var(--text-tertiary); font-size:11.5px; line-height:1.7; text-align:center; padding:14px 4px; }
  .host-controls { display:flex; flex-direction:column; align-items:flex-start; gap:8px; }
  .ghost-btn { padding:7px 11px; color:var(--text-error); font-size:11px; font-weight:600; background:#fef2f2; border:1px solid #fee2e2; border-radius:var(--radius-soft); cursor:pointer; width:100%; text-align:left; transition:all .2s cubic-bezier(0.16,1,0.3,1); /* Danger button */ }
  .ghost-btn:hover { color:#fff; background:var(--danger); border-color:var(--danger); }
  .disk-note { color:var(--text-tertiary); font-size:11px; }
  .version-badge { margin-top:auto; color:var(--muted); font-size:10px; text-align:center; padding-top:12px; border-top:1px solid var(--line); }
  .feed { flex:1; overflow-y:auto; padding:14px 20px 20px; -webkit-overflow-scrolling:touch; }
  .star-btn { display:grid; place-items:center; width:34px; height:34px; padding:0; border:0; border-radius:var(--radius-soft); cursor:pointer; color:var(--text-secondary); background:transparent; transition:all .2s cubic-bezier(0.16,1,0.3,1); }
  .star-btn:hover { color:#f5b400; background:#fffaf0; transform:scale(1.08); }
  .star-btn:active { transform:scale(0.95); }
  .star-btn svg { width:16px; fill:none; }
  .star-btn.starred { color:#f5b400; background:#fffaf0; }
  .star-btn.starred svg { fill:#f5b400; }
  .main.drag-over::after { content:'松开即可发送到对话'; position:absolute; inset:8px; z-index:5; display:flex; align-items:center;
    justify-content:center; background:rgba(38,103,255,.07); border:2px dashed var(--blue2); color:var(--blue);
    font-weight:750; font-size:14px; pointer-events:none; border-radius:16px; }
  ul { list-style:none; padding:0; margin:0; display:flex; flex-direction:column; gap:16px; }
  li.msg { display:flex; flex-direction:column; gap:5px; }
  .msg-meta { align-self:center; color:var(--text-secondary); font-size:11px; }
  .msg-row { display:flex; }
  li.msg.other .msg-row { justify-content:flex-start; }
  li.msg.mine .msg-row { justify-content:flex-end; }
  .bubble { max-width:78%; min-width:0; padding:11px 14px; border-radius:16px; background:#eef1f6; }
  li.msg.other .bubble { border-bottom-left-radius:4px; }
  li.msg.mine .bubble { border-bottom-right-radius:4px; background:linear-gradient(135deg,var(--blue2),var(--blue)); }
  li.msg.mine .bubble .fname, li.msg.mine .bubble .tcontent { color:#fff; }
  li.msg.mine .bubble .meta { color:rgba(255,255,255,.85); }
  li.msg.ghost .bubble { background:#f2efe7; }
  .file-bubble { display:flex; flex-direction:column; gap:9px; }
  .file-bubble-top { display:flex; align-items:center; gap:11px; min-width:0; }
  .ghost .fname { color:var(--muted); }
  .ghost .file-icon { opacity:.65; }
  .media-preview video { display:block; width:auto; height:auto; max-width:180px; max-height:150px; border-radius:10px; background:#000; }
  .media-preview audio { display:block; width:220px; max-width:100%; height:34px; }
  .media-thumb { flex:0 0 auto; width:42px; height:42px; border-radius:var(--radius-soft); overflow:hidden; background:#f0f3f8; }
  .media-thumb img { width:100%; height:100%; object-fit:cover; display:block; }
  .file-icon { display:grid; place-items:center; flex:0 0 auto; width:42px; height:42px; color:var(--text-secondary); background:#f0f3f8; border-radius:var(--radius-soft); }
  .file-icon svg { width:20px; }
  .fmeta { flex:1; min-width:0; }
  .fname { overflow:hidden; color:var(--text-primary); font-size:14px; font-weight:600; text-overflow:ellipsis; white-space:nowrap; }
  .tcontent { color:var(--text-primary); font-size:13.5px; line-height:1.6; white-space:pre-wrap; word-break:break-word; max-height:150px; overflow:auto; }
  .meta { color:var(--text-secondary); font-size:11px; margin-top:5px; font-weight:500; }
  .expiry.soon { color:var(--text-error); font-weight:650; }
  li.msg.mine .bubble .expiry.soon { color:#ffd7d8; }
  .actions { display:flex; gap:6px; }
  li.msg.other .actions { justify-content:flex-start; }
  li.msg.mine .actions { justify-content:flex-end; }
  .dl { display:grid; place-items:center; width:34px; height:34px; padding:0; border:0; border-radius:var(--radius-soft); cursor:pointer; transition:all .2s cubic-bezier(0.16,1,0.3,1); color:var(--text-interactive); background:#edf3ff; text-decoration:none; /* btn-icon.primary */ }
  .dl:hover { color:var(--text-inverse); background:var(--blue); transform:scale(1.05); }
  .dl:active { transform:scale(0.98); }
  .del { display:grid; place-items:center; width:34px; height:34px; padding:0; border:0; border-radius:var(--radius-soft); cursor:pointer; transition:all .2s cubic-bezier(0.16,1,0.3,1); color:#dc2626; background:#fef2f2; /* btn-icon.danger */ }
  .del:hover { color:var(--text-inverse); background:var(--danger); transform:scale(1.05); }
  .del:active { transform:scale(0.98); }
  .copy-text { display:grid; place-items:center; width:34px; height:34px; padding:0; border:0; border-radius:var(--radius-soft); cursor:pointer; color:var(--text-secondary); background:transparent; transition:all .2s cubic-bezier(0.16,1,0.3,1); }
  .copy-text:hover { color:var(--green); background:rgba(20,184,122,.1); }
  .copy-text:active { transform:scale(0.95); }
  .copy-text.copied { color:#fff; background:var(--green); }
  .dl svg,.del svg { width:16px; }
  .empty { display:grid; place-items:center; min-height:200px; color:var(--muted); text-align:center; font-size:13px; }
  .empty svg { width:38px; color:#b3bdcb; margin-bottom:10px; }
  .composer { flex:none; border-top:1px solid var(--line); padding:8px 12px calc(8px + env(safe-area-inset-bottom)); background:#fff; }
  .upload-queue:empty { display:none; }
  .qitem { padding:8px 4px; font-size:11.5px; }
  .qitem .top { display:flex; justify-content:space-between; gap:12px; }
  .qitem .name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .qitem .state { color:var(--muted); white-space:nowrap; }
  .qbar { height:4px; margin-top:6px; overflow:hidden; background:#e7ebf1; border-radius:99px; }
  .qbar div { width:0; height:100%; background:linear-gradient(90deg,var(--blue2),var(--blue)); border-radius:99px; transition:width .15s; }
  .composer-row { display:flex; align-items:flex-end; gap:8px; }
  .attach-btn { flex:none; display:grid; place-items:center; width:44px; height:44px; border-radius:var(--radius-soft);
    border:1px solid var(--line); background:transparent; color:var(--text-secondary); cursor:pointer; transition:all .2s cubic-bezier(0.16,1,0.3,1); /* btn-icon.secondary */ }
  .attach-btn:hover { border-color:var(--blue2); color:var(--text-interactive); background:#f0f5ff; }
  .attach-btn:active { transform:scale(0.96); }
  .attach-btn svg { width:20px; }
  .composer textarea { flex:1; min-height:64px; max-height:200px; padding:13px 16px; font:inherit; font-size:14.5px; line-height:1.6;
    color:var(--text-primary); border:1px solid var(--line); border-radius:var(--radius-soft); background:#fafcff; resize:none; }
  .composer textarea::placeholder { color:var(--text-tertiary); }
  .composer textarea:focus { outline:none; border-color:var(--blue2); box-shadow:0 0 0 3px rgba(38,103,255,.1); }
  .send-btn { flex:none; padding:0 20px; height:44px; color:var(--text-inverse); font-weight:600; font-size:13px; border:0; border-radius:var(--radius-soft);
    background:linear-gradient(135deg,var(--blue2),var(--blue)); cursor:pointer; transition:all .2s cubic-bezier(0.16,1,0.3,1); /* Primary button */ }
  .send-btn:hover:not(:disabled) { transform:translateY(-1px); box-shadow:0 6px 20px rgba(38,103,255,.3); }
  .send-btn:active:not(:disabled) { transform:translateY(0); }
  .send-btn:disabled { background:#e8ecf2; color:var(--text-disabled); cursor:not-allowed; }
  :focus-visible { outline:3px solid rgba(38,103,255,.25); outline-offset:2px; }
  /* ===== 按钮系统（6 类） ===== */
  button, .btn { font:inherit; transition:all .2s cubic-bezier(0.16,1,0.3,1); }
  /* 1. Primary - 主要行动（蓝色渐变） */
  .btn-primary { padding:9px 18px; color:var(--text-inverse); background:linear-gradient(135deg,var(--blue2),var(--blue)); border:0; border-radius:var(--radius-soft); cursor:pointer; font-weight:600; }
  .btn-primary:hover { transform:translateY(-1px); box-shadow:0 6px 20px rgba(38,103,255,.3); }
  .btn-primary:active { transform:translateY(0); }
  .btn-primary:disabled { background:#e8ecf2; color:var(--text-disabled); cursor:not-allowed; }
  /* 2. Secondary - 次要行动（灰色） */
  .btn-secondary { padding:9px 18px; color:var(--text-secondary); background:#f0f3f8; border:1px solid var(--line); border-radius:var(--radius-soft); cursor:pointer; font-weight:600; }
  .btn-secondary:hover { color:var(--text-primary); background:#e8ecf2; border-color:#d1d5dd; }
  .btn-secondary:active { transform:scale(0.98); }
  .btn-secondary:disabled { color:var(--text-disabled); background:#f9fafb; border-color:#f0f0f0; cursor:not-allowed; }
  /* 3. Tertiary - 低优先级（仅文字） */
  .btn-tertiary { padding:9px 18px; color:var(--text-secondary); background:transparent; border:0; border-radius:var(--radius-soft); cursor:pointer; font-weight:600; }
  .btn-tertiary:hover { color:var(--text-primary); background:rgba(0,0,0,.02); }
  .btn-tertiary:active { transform:scale(0.98); }
  .btn-tertiary:disabled { color:var(--text-disabled); cursor:not-allowed; }
  /* 4. Danger - 删除/危险操作（红色） */
  .btn-danger { padding:9px 18px; color:#fff; background:var(--danger); border:0; border-radius:var(--radius-soft); cursor:pointer; font-weight:600; }
  .btn-danger:hover { transform:translateY(-1px); box-shadow:0 6px 20px rgba(229,72,77,.3); }
  .btn-danger:active { transform:translateY(0); }
  .btn-danger:disabled { background:#f7f1f2; color:var(--text-disabled); cursor:not-allowed; }
  /* 5. Success - 成功/完成（绿色） */
  .btn-success { padding:9px 18px; color:#fff; background:var(--green); border:0; border-radius:var(--radius-soft); cursor:pointer; font-weight:600; }
  .btn-success:hover { transform:translateY(-1px); box-shadow:0 6px 20px rgba(20,184,122,.3); }
  .btn-success:active { transform:translateY(0); }
  .btn-success:disabled { background:#f0fdf4; color:var(--text-disabled); cursor:not-allowed; }
  /* 6. Ghost - 可选操作（仅边框） */
  .btn-ghost { padding:9px 18px; color:var(--text-secondary); background:transparent; border:1px solid var(--line); border-radius:var(--radius-soft); cursor:pointer; font-weight:600; }
  .btn-ghost:hover { color:var(--text-primary); border-color:var(--muted); background:rgba(0,0,0,.02); }
  .btn-ghost:active { transform:scale(0.98); }
  .btn-ghost:disabled { color:var(--text-disabled); border-color:#f0f0f0; cursor:not-allowed; }
  /* 小按钮（icon buttons） */
  .btn-icon { display:grid; place-items:center; padding:0; border:0; cursor:pointer; transition:all .2s cubic-bezier(0.16,1,0.3,1); }
  .btn-icon.primary { color:var(--text-interactive); background:#edf3ff; border-radius:var(--radius-soft); width:34px; height:34px; }
  .btn-icon.primary:hover { color:#fff; background:var(--blue); transform:scale(1.05); }
  .btn-icon.primary:active { transform:scale(0.98); }
  .btn-icon.danger { color:#dc2626; background:#fef2f2; border-radius:var(--radius-soft); width:34px; height:34px; }
  .btn-icon.danger:hover { color:#fff; background:var(--danger); transform:scale(1.05); }
  .btn-icon.danger:active { transform:scale(0.98); }
  .btn-icon.secondary { color:var(--text-secondary); background:transparent; border:1px solid var(--line); border-radius:var(--radius-soft); width:44px; height:44px; }
  .btn-icon.secondary:hover { color:var(--text-interactive); border-color:var(--blue2); background:#f0f5ff; }
  .btn-icon.secondary:active { transform:scale(0.96); }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0f1419; }
    body { background:#0f1419; }
    .sidebar { background:#1a1f2e; border-right-color:rgba(255,255,255,.05); }
    .app { background:#131820; box-shadow:0 0 40px rgba(0,0,0,.4); }
    .main { background:#0f1419; }
    .connect-block { background:#1a1f2e; border-color:rgba(255,255,255,.1); }
    .tab,.time-tab { background:#1a1f2e; border-color:rgba(255,255,255,.1); color:var(--text-secondary); }
    .tab:hover,.time-tab:hover { background:#262d3d; border-color:var(--blue2); }
    .sender-btn { background:#1a1f2e; border-color:rgba(255,255,255,.1); }
    .sender-btn:hover { background:#262d3d; }
    .search-input { background:#1a1f2e; border-color:rgba(255,255,255,.1); color:var(--text-primary); }
    .composer { background:#131820; border-top-color:rgba(255,255,255,.05); }
    .composer textarea { background:#1a1f2e; border-color:rgba(255,255,255,.1); color:var(--text-primary); }
    .bubble { background:#262d3d; }
    li.msg.mine .bubble { background:linear-gradient(135deg,var(--blue2),var(--blue)); }
    .attach-btn { border-color:rgba(255,255,255,.1); background:transparent; }
    .attach-btn:hover { background:#262d3d; border-color:var(--blue2); }
    .file-icon { background:#262d3d; }
    .media-thumb { background:#262d3d; }
    .dl { background:rgba(91,140,255,.15); }
    .dl:hover { background:var(--blue); }
    .del { background:rgba(229,72,77,.15); }
    .del:hover { background:var(--danger); }
    .ghost-btn { background:#2a1f23; border-color:#4a3436; }
    .ghost-btn:hover { background:var(--danger); border-color:var(--danger); }
    .mini-btn { background:#1a1f2e; border-color:rgba(255,255,255,.1); }
    .mini-btn:hover { background:#262d3d; border-color:rgba(255,255,255,.2); }
    .star-btn:hover { background:rgba(245,180,0,.15); }
    .copy-text:hover { background:rgba(20,184,122,.15); }
  }
  .mobile-topbar { display:none; }
  .drawer-backdrop { display:none; }
  @media (max-width:720px) {
    .mobile-topbar { flex:none; display:flex; align-items:center; gap:10px; padding:10px 14px; border-bottom:1px solid var(--line); }
    .menu-btn { flex:none; display:grid; place-items:center; width:36px; height:36px; border-radius:10px;
      border:1px solid var(--line); background:#fafcff; color:#62708a; cursor:pointer; }
    .menu-btn svg { width:18px; }
    .mobile-brand { font-weight:750; font-size:15px; letter-spacing:-.02em; }
    .sidebar { position:fixed; flex:none; top:0; left:0; width:82%; max-width:300px; z-index:60;
      height:100vh; height:-webkit-fill-available; height:calc(var(--vh, 1vh) * 100); height:100dvh;
      transform:translateX(-100%); transition:transform .25s ease; box-shadow:12px 0 30px rgba(20,30,50,.15); }
    .sidebar.open { transform:translateX(0); }
    .drawer-backdrop.show { display:block; position:fixed; inset:0; background:rgba(15,23,42,.35); z-index:55; }
  }
  @media (max-width:560px) {
    .sidebar { width:88%; padding:14px; gap:12px; }
    .feed { padding:10px 14px 16px; }
  }
  /* ===== 同步文件夹面板 ===== */
  .sync-folders-block { margin-top:14px; }
  .sync-folders-title { font-size:12.5px; font-weight:600; color:var(--text-secondary); letter-spacing:.04em; text-transform:uppercase; margin-bottom:8px; padding:0 4px; }
  .sync-folders-list { display:flex; flex-direction:column; gap:4px; }
  .sync-folder-btn { padding:8px 10px; text-align:left; font-size:13px; color:var(--text-primary); background:#f9fafb; border:1px solid var(--line);
    border-radius:var(--radius-soft); cursor:pointer; transition:all .2s cubic-bezier(0.16,1,0.3,1); }
  .sync-folder-btn:hover { background:#f0f3f8; border-color:var(--blue2); color:var(--text-interactive); }
  .sync-folder-btn.active { background:#edf3ff; border-color:var(--blue); color:var(--blue); font-weight:600; }
  .sync-folder-item { display:flex; justify-content:space-between; align-items:center; gap:8px; }
  .sync-folder-name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; flex:1; }
  .sync-folder-count { font-size:11px; color:var(--text-tertiary); white-space:nowrap; }
  .sync-folder-actions { display:flex; gap:2px; }
  .sync-add-btn { padding:6px 12px; font-size:12px; color:var(--text-interactive); background:transparent; border:1px dashed var(--blue); border-radius:var(--radius-soft);
    cursor:pointer; font-weight:600; transition:all .2s cubic-bezier(0.16,1,0.3,1); }
  .sync-add-btn:hover { background:#edf3ff; border-style:solid; }
  @media (prefers-color-scheme: dark) {
    .sync-folders-block { }
    .sync-folder-btn { background:#1a1f2e; border-color:rgba(255,255,255,.1); color:var(--text-primary); }
    .sync-folder-btn:hover { background:#262d3d; border-color:var(--blue2); }
    .sync-folder-btn.active { background:#1a2844; border-color:var(--blue); color:var(--blue); }
    .sync-add-btn { border-color:rgba(91,140,255,.4); }
    .sync-add-btn:hover { background:rgba(91,140,255,.1); }
  }
  @media (prefers-reduced-motion:reduce) { * { scroll-behavior:auto!important; transition:none!important; } }
</style>
</head>
<body>
<div class="app">
  <div id="drawerBackdrop" class="drawer-backdrop"></div>
  <aside class="sidebar">
    <div class="brand">
      <span class="logo"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12.6a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0M12 20h.01M2 9a14.4 14.4 0 0 1 20 0" stroke-linecap="round"/></svg></span>
      <span>sendfiledrop</span>
    </div>
    <div class="online-pill"><span class="dot"></span><span id="onlineCount">1</span> 台设备在线</div>

    <div class="connect-block">
      <div class="qr-big">__QR__</div>
      <div class="connect-info">
        <span id="urlText">__URL__</span>
        <button id="copyUrl" class="mini-btn">复制地址</button>
      </div>
    </div>

    <button id="nicknamePill" class="nickname-pill" title="点击修改昵称"></button>
    <input id="searchInput" class="search-input" placeholder="搜索文件名 / 文字…">

    <div class="senders-block">
      <div class="senders-title">最近活跃</div>
      <div id="sendersList" class="senders-list"></div>
    </div>

    <div class="favorites-block">
      <div class="favorites-title">收藏</div>
      <div id="favoritesList" class="favorites-list">__FAVORITES__</div>
    </div>

    <div id="syncFoldersBlock" class="sync-folders-block" style="display:none;">
      <div class="sync-folders-title">同步文件夹</div>
      <div id="syncFoldersList" class="sync-folders-list"></div>
    </div>

    <div class="host-controls">__HOST_CONTROLS__</div>
    <div class="version-badge">v__VERSION__</div>
  </aside>

  <main class="main">
    <div class="mobile-topbar">
      <button id="menuToggle" class="menu-btn" aria-label="菜单">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 7h16M4 12h16M4 17h16" stroke-linecap="round"/></svg>
      </button>
      <span class="mobile-brand">sendfiledrop</span>
    </div>
    <div class="filter-bar">
      <button class="tab active" data-tab="all">全部</button>
      <button class="tab" data-tab="text">文字列表</button>
      <button class="tab" data-tab="media">多媒体列表</button>
      <button class="tab" data-tab="file">文件列表</button>
      <div class="time-filter-sep"></div>
      <button class="time-tab" data-time="all">全部时间</button>
      <button class="time-tab" data-time="today">今天</button>
      <button class="time-tab" data-time="week">本周</button>
      <button class="time-tab" data-time="month">本月</button>
    </div>

    <div id="feed" class="feed">__FEED__</div>

    <footer class="composer">
      <div id="uploadQueue" class="upload-queue"></div>
      <div class="composer-row">
        <button id="attachBtn" class="attach-btn" title="发送文件" aria-label="发送文件">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.5 12.5 21a5 5 0 0 1-7-7L14 5.5a3.5 3.5 0 0 1 5 5L10.5 19a2 2 0 0 1-3-3L15 8.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </button>
        <textarea id="textInput" rows="1" placeholder="输入文字，Enter 发送，Shift+Enter 换行，或直接把文件拖进来…"></textarea>
        <button id="sendBtn" class="send-btn">发送</button>
      </div>
      <input id="fileInput" type="file" multiple style="display:none">
      <input id="reshareInput" type="file" style="display:none">
    </footer>
  </main>
</div>

<script>var DEFAULT_NAME = "__DEFAULT_NAME__";</script>
<script>
(function () {
  // 兼容移动端浏览器地址栏/工具栏动态显隐：用 innerHeight 算出真实可视高度，
  // 供不支持 100dvh 的浏览器 (老版 WeChat 内置浏览器等) 兜底使用。
  function setVh() {
    document.documentElement.style.setProperty('--vh', window.innerHeight * 0.01 + 'px');
  }
  setVh();
  window.addEventListener('resize', setVh);
  window.addEventListener('orientationchange', function () { setTimeout(setVh, 250); });

  var feedEl = document.getElementById('feed');
  var favoritesList = document.getElementById('favoritesList');
  var sendersList = document.getElementById('sendersList');
  var tabs = document.querySelectorAll('.tab');
  var searchInput = document.getElementById('searchInput');
  var textInput = document.getElementById('textInput');
  var sendBtn = document.getElementById('sendBtn');
  var attachBtn = document.getElementById('attachBtn');
  var fileInput = document.getElementById('fileInput');
  var reshareInput = document.getElementById('reshareInput');
  var uploadQueue = document.getElementById('uploadQueue');
  var onlineCountEl = document.getElementById('onlineCount');
  var nicknamePill = document.getElementById('nicknamePill');
  var clearTextsBtn = document.getElementById('clearTexts');
  var copyUrlBtn = document.getElementById('copyUrl');
  var menuToggle = document.getElementById('menuToggle');
  var sidebarEl = document.querySelector('.sidebar');
  var drawerBackdrop = document.getElementById('drawerBackdrop');

  var currentSenderFilter = null;

  function openDrawer() { sidebarEl.classList.add('open'); drawerBackdrop.classList.add('show'); }
  function closeDrawer() { sidebarEl.classList.remove('open'); drawerBackdrop.classList.remove('show'); }
  if (menuToggle) {
    menuToggle.onclick = function () {
      if (sidebarEl.classList.contains('open')) closeDrawer(); else openDrawer();
    };
  }
  drawerBackdrop.onclick = closeDrawer;

  var currentFilter = 'all';
  var currentSearch = '';
  var currentTimeFilter = 'all';
  var lastVersion = null;
  var pendingReshareId = null;
  var origTitle = document.title;

  function getNickname() {
    try { return localStorage.getItem('sfd_nickname') || ''; } catch (e) { return ''; }
  }
  function setNickname(name) {
    try { localStorage.setItem('sfd_nickname', name); } catch (e) {}
  }
  function nicknameHeader() {
    var n = getNickname();
    return n ? encodeURIComponent(n) : '';
  }
  function refreshNicknamePill() {
    nicknamePill.textContent = getNickname() || DEFAULT_NAME;
  }
  refreshNicknamePill();
  nicknamePill.onclick = function () {
    var cur = getNickname() || DEFAULT_NAME;
    var next = prompt('设置你的昵称', cur);
    if (next === null) return;
    next = next.trim();
    if (next) setNickname(next);
    refreshNicknamePill();
  };

  function getTimeRange(type) {
    var now = new Date();
    var start = new Date();
    switch(type) {
      case 'today':
        start.setHours(0, 0, 0, 0);
        return start.getTime() / 1000;
      case 'week':
        start.setDate(start.getDate() - start.getDay());
        start.setHours(0, 0, 0, 0);
        return start.getTime() / 1000;
      case 'month':
        start.setDate(1);
        start.setHours(0, 0, 0, 0);
        return start.getTime() / 1000;
      default:
        return 0;
    }
  }

  function applySearch() {
    var q = currentSearch.trim().toLowerCase();
    var items = feedEl.querySelectorAll('li[data-search]');
    var minTime = getTimeRange(currentTimeFilter);
    items.forEach(function (li) {
      var s = li.getAttribute('data-search') || '';
      var sender = li.querySelector('.msg-meta');
      var senderName = sender ? sender.textContent.split(' · ')[0] : '';
      var meta = li.getAttribute('data-meta') || '';
      var ts = parseFloat(meta) || 0;
      var matchSearch = !q || s.indexOf(q) !== -1;
      var matchSender = !currentSenderFilter || senderName.indexOf(currentSenderFilter) !== -1;
      var matchTime = currentTimeFilter === 'all' || ts >= minTime;
      li.style.display = (matchSearch && matchSender && matchTime) ? '' : 'none';
    });
  }
  searchInput.oninput = function () {
    currentSearch = searchInput.value;
    applySearch();
  };

  function isFeedNearBottom() {
    return feedEl.scrollHeight - feedEl.scrollTop - feedEl.clientHeight < 80;
  }
  function scrollFeedToBottom() {
    feedEl.scrollTop = feedEl.scrollHeight;
  }
  function refreshFeed(forceBottom) {
    var stick = forceBottom || isFeedNearBottom();
    fetch('/feed?filter=' + encodeURIComponent(currentFilter))
      .then(function (r) { return r.text(); })
      .then(function (htmlStr) {
        feedEl.innerHTML = htmlStr;
        applySearch();
        updateExpiryLabels();
        if (stick) scrollFeedToBottom();
      });
  }

  function refreshFavorites() {
    fetch('/favorites')
      .then(function (r) { return r.text(); })
      .then(function (htmlStr) { favoritesList.innerHTML = htmlStr; updateExpiryLabels(); });
  }

  function refreshSenders() {
    fetch('/senders')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var html = '';
        if (data.senders && data.senders.length > 0) {
          data.senders.forEach(function (sender) {
            var active = currentSenderFilter === sender.name ? ' active' : '';
            html += '<button class="sender-btn' + active + '" data-sender="' + sender.name.replace(/"/g, '&quot;') + '">' + sender.name + '</button>';
          });
        }
        sendersList.innerHTML = html;
        attachSenderListeners();
      });
  }

  function attachSenderListeners() {
    var senderBtns = sendersList.querySelectorAll('.sender-btn');
    senderBtns.forEach(function (btn) {
      btn.onclick = function () {
        var sender = btn.getAttribute('data-sender');
        if (currentSenderFilter === sender) {
          currentSenderFilter = null;
        } else {
          currentSenderFilter = sender;
        }
        refreshSenders();
        applySearch();
      };
    });
  }

  function formatRemaining(seconds) {
    if (seconds <= 0) return '即将清除';
    var days = Math.floor(seconds / 86400);
    var hours = Math.floor((seconds % 86400) / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    if (days >= 1) return days + '天' + hours + '小时后清除';
    if (hours >= 1) return hours + '小时' + minutes + '分钟后清除';
    if (minutes >= 1) return minutes + '分钟后清除';
    return '即将清除';
  }
  function updateExpiryLabels() {
    var now = Date.now() / 1000;
    document.querySelectorAll('.expiry[data-expire]').forEach(function (el) {
      var remaining = parseFloat(el.getAttribute('data-expire')) - now;
      el.textContent = formatRemaining(remaining);
      el.classList.toggle('soon', remaining > 0 && remaining < 3600);
    });
  }
  setInterval(updateExpiryLabels, 30000);

  tabs.forEach(function (tab) {
    tab.onclick = function () {
      tabs.forEach(function (t) { t.classList.remove('active'); });
      tab.classList.add('active');
      currentFilter = tab.getAttribute('data-tab');
      refreshFeed(currentFilter === 'all' || currentFilter === 'text');
    };
  });

  var timeTabs = document.querySelectorAll('.time-tab');
  timeTabs.forEach(function (tab) {
    tab.onclick = function () {
      timeTabs.forEach(function (t) { t.classList.remove('active'); });
      tab.classList.add('active');
      currentTimeFilter = tab.getAttribute('data-time');
      applySearch();
    };
  });
  if (timeTabs.length > 0) timeTabs[0].classList.add('active');

  function beep() {
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      var ctx = new Ctx();
      var o = ctx.createOscillator(), g = ctx.createGain();
      o.type = 'sine'; o.frequency.value = 880;
      o.connect(g); g.connect(ctx.destination);
      g.gain.setValueAtTime(0.16, ctx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.3);
      o.start(); o.stop(ctx.currentTime + 0.3);
    } catch (e) {}
  }
  function notifyNewContent() {
    beep();
    var n = 0;
    var iv = setInterval(function () {
      document.title = (document.title === origTitle) ? '新内容 · sendfiledrop' : origTitle;
      n++;
      if (n >= 6) { clearInterval(iv); document.title = origTitle; }
    }, 500);
  }

  function poll() {
    fetch('/status').then(function (r) { return r.json(); }).then(function (data) {
      onlineCountEl.textContent = data.online;
      if (lastVersion !== null && data.version !== lastVersion) {
        notifyNewContent();
        refreshFeed();
        refreshFavorites();
        refreshSenders();
        refreshSyncFolders();
      }
      lastVersion = data.version;
    }).catch(function () {});
  }
  scrollFeedToBottom();
  window.addEventListener('load', scrollFeedToBottom);
  refreshSenders();
  refreshSyncFolders();
  poll();
  setInterval(poll, 3000);

  function escapeHtml(text) {
    var map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return text.replace(/[&<>"']/g, function (m) { return map[m]; });
  }

  function doSendText() {
    var val = textInput.value;
    if (!val.trim()) return;
    sendBtn.disabled = true;
    fetch('/text', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8', 'X-Nickname': nicknameHeader() },
      body: 'content=' + encodeURIComponent(val)
    }).then(function (res) {
      sendBtn.disabled = false;
      if (res.ok) { textInput.value = ''; textInput.style.height = ''; refreshFeed(true); }
      else { alert('发送失败'); }
    }).catch(function () { sendBtn.disabled = false; alert('发送失败'); });
  }
  sendBtn.onclick = doSendText;
  textInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doSendText(); }
  });
  textInput.addEventListener('input', function () {
    textInput.style.height = 'auto';
    textInput.style.height = Math.min(textInput.scrollHeight, 200) + 'px';
  });

  attachBtn.onclick = function () { fileInput.click(); };
  fileInput.onchange = function () {
    var files = Array.prototype.slice.call(fileInput.files);
    fileInput.value = '';
    if (files.length) uploadFiles(files);
  };

  // 文件可以直接拖进右侧对话区发送
  var mainEl = document.querySelector('.main');
  var dragCounter = 0;
  mainEl.addEventListener('dragenter', function (e) { e.preventDefault(); dragCounter++; mainEl.classList.add('drag-over'); });
  mainEl.addEventListener('dragover', function (e) { e.preventDefault(); });
  mainEl.addEventListener('dragleave', function (e) {
    e.preventDefault();
    dragCounter = Math.max(0, dragCounter - 1);
    if (dragCounter === 0) mainEl.classList.remove('drag-over');
  });
  mainEl.addEventListener('drop', function (e) {
    e.preventDefault();
    dragCounter = 0;
    mainEl.classList.remove('drag-over');
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
      uploadFiles(Array.prototype.slice.call(e.dataTransfer.files));
    }
  });

  // 剪切板粘贴图片功能：在编辑区任何地方 Ctrl+V 粘贴图片
  document.addEventListener('paste', function (e) {
    if (!e.clipboardData || !e.clipboardData.items) return;
    var files = [];
    for (var i = 0; i < e.clipboardData.items.length; i++) {
      var item = e.clipboardData.items[i];
      if (item.kind === 'file') {
        var file = item.getAsFile();
        if (file) files.push(file);
      }
    }
    if (files.length > 0) {
      e.preventDefault();
      uploadFiles(files);
    }
  });

  function uploadFiles(files) {
    var i = 0;
    function next() {
      if (i >= files.length) { refreshFeed(true); return; }
      uploadOne(files[i], function () { i++; next(); });
    }
    next();
  }
  function uploadOne(f, done) {
    var row = document.createElement('div');
    row.className = 'qitem';
    row.innerHTML = '<div class="top"><span class="name"></span><span class="state">上传中…</span></div><div class="qbar"><div></div></div>';
    row.querySelector('.name').textContent = f.name;
    uploadQueue.appendChild(row);
    var barIn = row.querySelector('.qbar div');
    var state = row.querySelector('.state');
    var fd = new FormData();
    fd.append('file', f);
    var xhr = new XMLHttpRequest();
    xhr.open('POST', '/upload');
    xhr.setRequestHeader('X-Nickname', nicknameHeader());
    xhr.upload.onprogress = function (e) {
      if (e.lengthComputable) {
        var p = Math.round(e.loaded / e.total * 100);
        barIn.style.width = p + '%';
        state.textContent = p + '%';
      }
    };
    xhr.onload = function () {
      state.textContent = (xhr.status >= 200 && xhr.status < 400) ? '完成' : '失败';
      setTimeout(function () { row.remove(); }, 900);
      done();
    };
    xhr.onerror = function () { state.textContent = '失败'; setTimeout(function () { row.remove(); }, 900); done(); };
    xhr.send(fd);
  }

  function handleListClick(e) {
    if (e.currentTarget === favoritesList && window.innerWidth <= 720 && (e.target.closest('.del') || e.target.closest('.reshare-btn') || e.target.closest('a.dl'))) {
      closeDrawer();
    }
    var starBtn = e.target.closest('.star-btn');
    if (starBtn) {
      var skind = starBtn.getAttribute('data-kind');
      var skey = starBtn.getAttribute('data-key');
      starBtn.disabled = true;
      fetch('/star/' + skind + '/' + encodeURIComponent(skey), { method: 'POST' }).then(function (res) {
        starBtn.disabled = false;
        if (res.ok) { refreshFeed(); refreshFavorites(); }
      }).catch(function () { starBtn.disabled = false; });
      return;
    }
    var copyBtn = e.target.closest('.copy-text');
    if (copyBtn) {
      var content = copyBtn.getAttribute('data-content') || '';
      if (navigator.clipboard) navigator.clipboard.writeText(content);
      copyBtn.classList.add('copied');
      setTimeout(function () { copyBtn.classList.remove('copied'); }, 1200);
      return;
    }
    var delBtn = e.target.closest('.del');
    if (delBtn) {
      var kind = delBtn.getAttribute('data-kind');
      var key = delBtn.getAttribute('data-key');
      var label = kind === 'text' ? '这条文字' : '这个文件';
      if (!confirm('确定删除' + label + '吗？')) return;
      delBtn.disabled = true;
      var url = (kind === 'text' ? '/text/delete/' : '/file/delete/') + encodeURIComponent(key);
      fetch(url, { method: 'POST' }).then(function (res) {
        if (res.ok) { refreshFeed(); refreshFavorites(); }
        else { alert('删除失败'); delBtn.disabled = false; }
      }).catch(function () { alert('删除失败'); delBtn.disabled = false; });
      return;
    }
    var reBtn = e.target.closest('.reshare-btn');
    if (reBtn) {
      pendingReshareId = reBtn.getAttribute('data-key');
      reshareInput.click();
    }
  }
  feedEl.addEventListener('click', handleListClick);
  favoritesList.addEventListener('click', handleListClick);

  reshareInput.onchange = function () {
    var f = reshareInput.files[0];
    reshareInput.value = '';
    if (!f || !pendingReshareId) return;
    var fd = new FormData();
    fd.append('file', f);
    fetch('/file/reshare/' + encodeURIComponent(pendingReshareId), {
      method: 'POST', body: fd, headers: { 'X-Nickname': nicknameHeader() }
    }).then(function (res) {
      if (res.ok) { refreshFeed(); refreshFavorites(); } else alert('重新分享失败');
    }).catch(function () { alert('重新分享失败'); });
  };

  if (clearTextsBtn) {
    clearTextsBtn.onclick = function () {
      if (!confirm('确定清空所有文字消息吗？此操作不可恢复。')) return;
      fetch('/texts/clear', { method: 'POST' }).then(function (res) {
        if (res.ok) { refreshFeed(); refreshFavorites(); } else alert('清空失败');
      });
    };
  }

  if (copyUrlBtn) {
    copyUrlBtn.onclick = function () {
      var t = document.getElementById('urlText').textContent;
      if (navigator.clipboard) navigator.clipboard.writeText(t);
      copyUrlBtn.textContent = '已复制';
      setTimeout(function () { copyUrlBtn.textContent = '复制'; }, 1200);
    };
  }
})();
</script>
</body>
</html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    # -- 基础工具 ------------------------------------------------------
    def _resolve_client_id(self):
        cookie = self.headers.get("Cookie", "")
        cid = None
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("cid="):
                cid = part[4:].strip()
                break
        if not cid:
            cid = uuid.uuid4().hex
            self._new_cid = cid
        else:
            self._new_cid = None
        self._client_id = cid

    def _is_host(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _nickname(self):
        raw = self.headers.get("X-Nickname")
        if raw:
            try:
                name = urllib.parse.unquote(raw).strip()
            except Exception:  # noqa: BLE001
                name = ""
            if name:
                return name[:40]
        return friendly_name_for(self._client_id)

    def _prepare(self):
        self._resolve_client_id()
        touch_presence(self._client_id)

    def _maybe_set_cookie(self):
        if getattr(self, "_new_cid", None):
            self.send_header("Set-Cookie", f"cid={self._new_cid}; Path=/; Max-Age=31536000; SameSite=Lax")

    def _send_html(self, body, code=200, head_only=False):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._maybe_set_cookie()
        self.end_headers()
        if not head_only:
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _send_json(self, obj, code=200, head_only=False):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._maybe_set_cookie()
        self.end_headers()
        if not head_only:
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def handle_one_request(self):
        # 客户端(尤其安卓下载器)频繁提前断开连接，静默处理避免刷屏
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # -- 页面渲染 --------------------------------------------------------
    def _render_page(self, head_only):
        ip = get_lan_ip()
        url = f"http://{ip}:{PORT}"
        is_host = self._is_host()
        feed_html = build_feed("all", self._client_id, is_host)
        favorites_html = build_favorites(self._client_id, is_host)
        default_name = friendly_name_for(self._client_id)
        version = load_version()
        host_controls = ""
        if is_host:
            disk_bytes = _dir_size(SHARE_DIR)
            host_controls = (
                '<button id="clearTexts" class="ghost-btn" title="清空所有文字消息">清空聊天记录</button>'
                f'<span class="disk-note">共享文件占用 {html.escape(human_size(disk_bytes))}</span>'
            )
        page = (
            PAGE_TEMPLATE
            .replace("__URL__", html.escape(url))
            .replace("__QR__", qr_svg_for(url))
            .replace("__FEED__", feed_html)
            .replace("__FAVORITES__", favorites_html)
            .replace("__DEFAULT_NAME__", html.escape(default_name))
            .replace("__HOST_CONTROLS__", host_controls)
            .replace("__VERSION__", html.escape(version))
        )
        self._send_html(page, head_only=head_only)

    def do_GET(self):
        self._handle_get(head_only=False)

    def do_HEAD(self):
        self._handle_get(head_only=True)

    def _handle_get(self, head_only):
        self._prepare()
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self._render_page(head_only)
            return

        if path == "/feed":
            filter_key = qs.get("filter", ["all"])[0]
            if filter_key not in ("all", "text", "media", "file"):
                filter_key = "all"
            frag = build_feed(filter_key, self._client_id, self._is_host())
            self._send_html(frag, head_only=head_only)
            return

        if path == "/status":
            self._send_json({"online": online_count(), "version": current_version()}, head_only=head_only)
            return

        if path == "/favorites":
            frag = build_favorites(self._client_id, self._is_host())
            self._send_html(frag, head_only=head_only)
            return

        if path == "/senders":
            senders = get_active_senders()
            self._send_json({"senders": senders}, head_only=head_only)
            return

        if path.startswith("/download/"):
            name = os.path.basename(path[len("/download/"):])
            touch_file_access(name)
            self._serve_file(name, head_only, inline=False)
            return

        if path.startswith("/view/"):
            name = os.path.basename(path[len("/view/"):])
            touch_file_access(name)
            self._serve_file(name, head_only, inline=True)
            return

        # ---- 同步文件夹路由 ----
        if sync_store and path == "/api/sync/folders":
            self._handle_sync_get_folders(head_only)
            return

        if sync_store and path == "/api/sync/manifest":
            folder_id = qs.get("folder", [None])[0]
            self._handle_sync_get_manifest(folder_id, head_only)
            return

        if sync_store and path.startswith("/api/sync/file"):
            folder_id = qs.get("folder", [None])[0]
            file_path = qs.get("path", [None])[0]
            self._handle_sync_download_file(folder_id, file_path, head_only)
            return

        if sync_store and path == "/sync_client.py":
            self._serve_sync_client(head_only)
            return

        self._send_html("<h1>404</h1>", 404, head_only=head_only)

    def _serve_file(self, name, head_only, inline=False):
        fpath = os.path.join(SHARE_DIR, name)
        if not os.path.isfile(fpath):
            self._send_html("<h1>404 文件不存在</h1>", 404, head_only=head_only)
            return

        size = os.path.getsize(fpath)
        start, end = 0, size - 1

        rng = self.headers.get("Range")
        is_partial = False
        if rng and rng.startswith("bytes="):
            try:
                s, _, e = rng[len("bytes="):].partition("-")
                if s.strip():
                    start = int(s)
                    end = int(e) if e.strip() else size - 1
                else:
                    start = max(0, size - int(e))
                    end = size - 1
                if start > end or start >= size:
                    raise ValueError
                is_partial = True
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return

        length = end - start + 1
        self.send_response(206 if is_partial else 200)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self.send_header("Content-Type", mime)
        disposition = "inline" if inline else "attachment"
        self.send_header("Content-Disposition", f"{disposition}; filename*=UTF-8''" + urllib.parse.quote(name))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if is_partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self._maybe_set_cookie()
        self.end_headers()

        if head_only:
            return

        try:
            with open(fpath, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- POST ------------------------------------------------------------
    def do_PUT(self):
        """Handle PUT requests (for file uploads in sync)"""
        self._prepare()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if sync_store and path.startswith("/api/sync/file"):
            folder_id = qs.get("folder", [None])[0]
            file_path = qs.get("path", [None])[0]
            base_rev = qs.get("base_rev", [None])[0]
            device = qs.get("device", [None])[0]
            self._handle_sync_upload_file(folder_id, file_path, base_rev, device)
            return

        self._send_html("<h1>404</h1>", 404)

    def do_DELETE(self):
        """Handle DELETE requests (for file deletion in sync)"""
        self._prepare()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if sync_store and path.startswith("/api/sync/file"):
            folder_id = qs.get("folder", [None])[0]
            file_path = qs.get("path", [None])[0]
            base_rev = qs.get("base_rev", [None])[0]
            device = qs.get("device", [None])[0]
            self._handle_sync_delete_sync_file(folder_id, file_path, base_rev, device)
            return

        self._send_html("<h1>404</h1>", 404)

    def do_POST(self):
        self._prepare()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/text":
            self._handle_add_text(); return
        if path.startswith("/text/delete/"):
            self._handle_delete_text(path[len("/text/delete/"):]); return
        if path == "/texts/clear":
            self._handle_clear_texts(); return
        if path == "/upload":
            self._handle_upload(); return
        if path.startswith("/file/delete/"):
            self._handle_delete_file(path[len("/file/delete/"):]); return
        if path.startswith("/file/reshare/"):
            self._handle_reshare(path[len("/file/reshare/"):]); return
        if path.startswith("/star/text/"):
            self._handle_star("text", path[len("/star/text/"):]); return
        if path.startswith("/star/file/"):
            self._handle_star("file", path[len("/star/file/"):]); return

        # ---- 同步文件夹路由 ----
        if sync_store and path == "/api/sync/folders":
            self._handle_sync_create_folder(); return
        if sync_store and path.startswith("/api/sync/folders/delete/"):
            folder_id = path[len("/api/sync/folders/delete/"):]
            self._handle_sync_delete_folder(folder_id); return

        self._send_html("<h1>404</h1>", 404)

    def _handle_star(self, kind, raw_id):
        item_id = urllib.parse.unquote(raw_id)
        if kind == "text":
            result = toggle_star_text(item_id, self._client_id)
        else:
            result = toggle_star_file(item_id, self._client_id)
        if result is None:
            self._send_html("<h1>404</h1>", 404)
            return
        self._send_json({"starred": result})

    def _handle_add_text(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", "replace")
        params = urllib.parse.parse_qs(body)
        content = params.get("content", [""])[0].strip()
        if not content:
            self._send_html("<h1>400 内容为空</h1>", 400)
            return
        add_text(content[:20000], self._nickname(), self._client_id)
        self._send_html("ok")

    def _handle_delete_text(self, raw_id):
        text_id = urllib.parse.unquote(raw_id)
        if not text_id:
            self._send_html("<h1>400</h1>", 400)
            return
        result = delete_text(text_id, self._client_id, self._is_host())
        if result == "notfound":
            self._send_html("<h1>404</h1>", 404); return
        if result == "forbidden":
            self._send_html("<h1>403 无权删除</h1>", 403); return
        self._send_html("ok")

    def _handle_clear_texts(self):
        if not self._is_host():
            self._send_html("<h1>403</h1>", 403)
            return
        clear_texts()
        self._send_html("ok")

    def _handle_upload(self):
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype or "boundary=" not in ctype:
            self._send_html("<h1>400 需要 multipart 表单</h1>", 400)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        boundary = ctype.split("boundary=", 1)[1].strip().strip('"')
        try:
            parts = self._parse_multipart(body, boundary)
        except Exception as e:  # noqa: BLE001
            self._send_html(f"<h1>500 上传解析失败: {html.escape(str(e))}</h1>", 500)
            return
        sender = self._nickname()
        for filename, content in parts:
            add_file(filename, content, sender, self._client_id)
        self._send_html("ok")

    def _handle_delete_file(self, raw_id):
        entry_id = urllib.parse.unquote(raw_id)
        result = delete_file(entry_id, self._client_id, self._is_host())
        if result == "notfound":
            self._send_html("<h1>404</h1>", 404); return
        if result == "forbidden":
            self._send_html("<h1>403 无权删除</h1>", 403); return
        self._send_html("ok")

    def _handle_reshare(self, raw_id):
        entry_id = urllib.parse.unquote(raw_id)
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype or "boundary=" not in ctype:
            self._send_html("<h1>400 需要 multipart 表单</h1>", 400)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        boundary = ctype.split("boundary=", 1)[1].strip().strip('"')
        try:
            parts = self._parse_multipart(body, boundary)
        except Exception as e:  # noqa: BLE001
            self._send_html(f"<h1>500 解析失败: {html.escape(str(e))}</h1>", 500)
            return
        if not parts:
            self._send_html("<h1>400 未选择文件</h1>", 400)
            return
        filename, content = parts[0]
        result = reshare_file(entry_id, content, self._nickname(), self._client_id)
        if result is None:
            self._send_html("<h1>404 记录不存在或未过期</h1>", 404)
            return
        self._send_html("ok")

    # ---- 同步文件夹处理函数 ----
    def _serve_sync_client(self, head_only):
        """GET /sync_client.py - 分发同步代理脚本"""
        script_path = os.path.join(BASE_DIR, "sync_client.py")
        if not os.path.isfile(script_path):
            self._send_html("<h1>404 sync_client.py 不存在</h1>", 404, head_only=head_only)
            return
        try:
            size = os.path.getsize(script_path)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", "attachment; filename=sync_client.py")
            self._maybe_set_cookie()
            self.end_headers()
            if not head_only:
                with open(script_path, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
        except (OSError, IOError):
            self._send_html("<h1>500</h1>", 500, head_only=head_only)

    def _handle_sync_get_folders(self, head_only):
        """GET /api/sync/folders - 获取所有同步文件夹列表"""
        folders = sync_store.get_folders()
        self._send_json({"folders": folders, "is_host": self._is_host()}, head_only=head_only)

    def _handle_sync_get_manifest(self, folder_id, head_only):
        """GET /api/sync/manifest?folder=<id> - 获取文件夹 manifest"""
        if not folder_id:
            self._send_json({"error": "Missing folder"}, code=400, head_only=head_only)
            return
        manifest, error = sync_store.get_manifest(folder_id)
        if error:
            self._send_json({"error": error}, code=404, head_only=head_only)
            return
        self._send_json({"manifest": manifest}, head_only=head_only)

    def _handle_sync_download_file(self, folder_id, rel_path, head_only):
        """GET /api/sync/file?folder=<id>&path=<path> - 下载同步文件"""
        if not folder_id or not rel_path:
            self._send_html("<h1>400</h1>", 400, head_only=head_only)
            return
        file_path, error = sync_store.read_file(folder_id, rel_path)
        if error:
            self._send_html(f"<h1>404</h1>", 404, head_only=head_only)
            return
        try:
            size = os.path.getsize(file_path)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(os.path.basename(rel_path))}")
            self._maybe_set_cookie()
            self.end_headers()
            if not head_only:
                with open(file_path, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
        except (OSError, IOError):
            self._send_html("<h1>500</h1>", 500, head_only=head_only)

    def _handle_sync_create_folder(self):
        """POST /api/sync/folders - 创建新同步文件夹（主机专属）"""
        if not self._is_host():
            self._send_html("<h1>403</h1>", 403)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = self.rfile.read(length).decode("utf-8", "replace")
            params = urllib.parse.parse_qs(body)
            name = params.get("name", [""])[0].strip()
            if not name:
                self._send_json({"error": "Name required"}, code=400)
                return
            folder_id, error = sync_store.create_folder(name)
            if error:
                self._send_json({"error": error}, code=400)
                return
            bump_version()
            self._send_json({"folder_id": folder_id, "name": name})
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": str(e)}, code=500)

    def _handle_sync_delete_folder(self, folder_id):
        """POST /api/sync/folders/delete/<id> - 删除同步文件夹（主机专属）"""
        if not self._is_host():
            self._send_html("<h1>403</h1>", 403)
            return
        error = sync_store.delete_folder(folder_id)
        if error:
            self._send_json({"error": error}, code=400)
            return
        bump_version()
        self._send_json({"ok": True})

    def _handle_sync_upload_file(self, folder_id, rel_path, base_rev, device=None):
        """PUT /api/sync/file?folder=<id>&path=<path>&base_rev=<rev>&device=<d> - 上传文件"""
        if not folder_id or not rel_path:
            self._send_json({"error": "Missing folder or path"}, code=400)
            return
        try:
            base_rev = int(base_rev) if base_rev else None
        except (ValueError, TypeError):
            base_rev = None

        length = int(self.headers.get("Content-Length", 0))
        if length < 0:
            self._send_json({"error": "Missing Content-Length"}, code=400)
            return

        class LimitedFileWrapper:
            """Wrapper to limit file-like object reads to Content-Length bytes"""
            def __init__(self, rfile, size):
                self.rfile = rfile
                self.remaining = size
            def read(self, sz=65536):
                if self.remaining <= 0:
                    return b""
                to_read = min(sz, self.remaining)
                data = self.rfile.read(to_read)
                self.remaining -= len(data)
                return data

        file_wrapper = LimitedFileWrapper(self.rfile, length)
        device = device or "host"
        success, error, new_rev = sync_store.write_file(folder_id, rel_path, file_wrapper, base_rev, expected_length=length, device=device)

        if not success:
            if "Conflict" in error or "mismatch" in error:
                self._send_json({"error": error, "current_rev": new_rev}, code=409)
            else:
                self._send_json({"error": error}, code=400)
            return

        bump_version()
        self._send_json({"ok": True, "rev": new_rev})

    def _handle_sync_delete_sync_file(self, folder_id, rel_path, base_rev, device=None):
        """DELETE /api/sync/file?folder=<id>&path=<path>&base_rev=<rev>&device=<d> - 删除文件"""
        if not folder_id or not rel_path:
            self._send_json({"error": "Missing folder or path"}, code=400)
            return
        try:
            base_rev = int(base_rev) if base_rev else None
        except (ValueError, TypeError):
            base_rev = None

        device = device or "host"
        success, error = sync_store.delete_file(folder_id, rel_path, base_rev, device)

        if not success:
            if "Conflict" in error or "mismatch" in error:
                self._send_json({"error": error}, code=409)
            else:
                self._send_json({"error": error}, code=400)
            return

        bump_version()
        self._send_json({"ok": True})

    # ---- multipart 解析 ----
    def _parse_multipart(self, body, boundary):
        """轻量 multipart 解析，不依赖已被移除的 cgi 模块。返回 [(filename, content), ...]"""
        delim = b"--" + boundary.encode("latin-1")
        parts = body.split(delim)
        results = []
        for part in parts:
            if part in (b"", b"--", b"--\r\n", b"\r\n"):
                continue
            if part.startswith(b"\r\n"):
                part = part[2:]
            if part.endswith(b"\r\n"):
                part = part[:-2]
            if b"\r\n\r\n" not in part:
                continue
            raw_headers, content = part.split(b"\r\n\r\n", 1)
            header_text = raw_headers.decode("utf-8", "replace")
            filename = None
            for line in header_text.split("\r\n"):
                if line.lower().startswith("content-disposition"):
                    for token in line.split(";"):
                        token = token.strip()
                        if token.startswith("filename="):
                            filename = token[len("filename="):].strip().strip('"')
            if not filename:
                continue
            safe = os.path.basename(filename)
            if not safe:
                continue
            results.append((safe, content))
        return results

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ip = get_lan_ip()
    server = ThreadingServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=sweep_loop, daemon=True).start()
    print("=" * 46)
    print("  sendfiledrop 已启动")
    print("=" * 46)
    print(f"  本机打开:   http://127.0.0.1:{PORT}")
    print(f"  局域网打开: http://{ip}:{PORT}")
    print(f"  共享目录:   {SHARE_DIR}")
    print("=" * 46)
    print("  按 Ctrl+C 停止服务")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        server.shutdown()


if __name__ == "__main__":
    main()
