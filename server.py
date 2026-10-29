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

# 先把脚本目录放进 sys.path：python3 -I 或从别的目录启动时，同目录模块也能被找到
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import atexit
import gzip
import hashlib
import html
import http.server
import json
import mimetypes
import re
import secrets
import shutil
import socketserver
import threading
import time
import urllib.parse
from config import (
    BASE_DIR,
    FEED_PAGE_SIZE,
    INLINE_SAFE_EXTS,
    LONGPOLL_SECONDS,
    MAX_UPLOAD_BYTES,
    PASSCODE,
    PORT,
    SHARE_DIR
)
from util import cached_dir_size, get_lan_ip, human_size, load_version, qr_svg_for
from store import (
    add_file,
    add_text,
    bump_version,
    cleanup_stale_uploads,
    clear_texts,
    current_version,
    delete_file,
    delete_text,
    flush_files_if_dirty,
    friendly_name_for,
    online_count,
    reshare_file,
    safe_share_path,
    sweep_loop,
    toggle_star_file,
    toggle_star_text,
    touch_file_access,
    touch_presence,
    wait_version_change
)
from render import build_favorites, build_feed, get_active_senders, load_page_template
from uploads import UploadError, read_multipart_files


try:
    import sync_store
except ImportError:
    sync_store = None


_STATIC_ASSETS = {
    "/static/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/static/vendor/zhaoui/zhaoui.min.css": ("vendor/zhaoui/zhaoui.min.css", "text/css; charset=utf-8"),
    "/static/vendor/zhaoui/zhaoui.min.js": ("vendor/zhaoui/zhaoui.min.js", "application/javascript; charset=utf-8"),
}


_static_cache = {}  # url path -> (stat signature, etag, raw bytes, gzip bytes or None)


def load_static(url_path):
    """静态资源缓存在内存里（文件变了会按 mtime 自动重载），预压缩 gzip。"""
    filename, _ = _STATIC_ASSETS[url_path]
    full = os.path.join(BASE_DIR, "static", filename)
    st = os.stat(full)
    sig = (st.st_mtime_ns, st.st_size)
    cached = _static_cache.get(url_path)
    if cached and cached[0] == sig:
        return cached
    with open(full, "rb") as f:
        raw = f.read()
    gz = gzip.compress(raw, 6) if len(raw) > 512 else None
    entry = (sig, '"%x-%x"' % sig, raw, gz)
    _static_cache[url_path] = entry
    return entry


_VALID_CID = re.compile(r"^[0-9a-f]{32}$")


_BOUNDARY_RE = re.compile(r'boundary=(?:"([^"]+)"|([^;\s]+))', re.I)


AUTH_TOKEN = hashlib.sha256(("sfd-auth:" + PASSCODE).encode("utf-8")).hexdigest() if PASSCODE else ""


LOGIN_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>SendFileDrop · 访问口令</title>
<style>body{font-family:system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;background:#f5f6f8}
form{background:#fff;padding:28px;border-radius:12px;box-shadow:0 2px 12px #0001;display:grid;gap:12px;width:min(320px,86vw)}
input,button{font-size:16px;padding:10px;border-radius:8px;border:1px solid #ccd}button{background:#2667ff;color:#fff;border:0}
.err{color:#d33;font-size:13px;margin:0}</style></head><body>
<form method="post" action="/login"><strong>请输入访问口令</strong>__ERR__
<input type="password" name="passcode" autofocus autocomplete="current-password"><button>进入</button></form></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # 长连接：避免轮询和静态资源每次都重新建连
    timeout = 120                   # 空闲或停滞的连接 2 分钟后回收，防止线程堆积

    # -- 基础工具 ------------------------------------------------------
    def send_response(self, code, message=None):
        super().send_response(code, message)
        # 出错时请求体多半没读完，长连接会错位，直接关掉
        if code >= 400 and self.command in ("POST", "PUT", "DELETE"):
            self.send_header("Connection", "close")

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        super().end_headers()

    def _resolve_client_id(self):
        cookies = {}
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k:
                cookies[k] = v.strip()
        self._cookies = cookies
        cid = cookies.get("cid", "")
        if _VALID_CID.match(cid):
            self._new_cid = None
        else:
            cid = secrets.token_hex(16)
            self._new_cid = cid
        self._client_id = cid

    def _is_host(self):
        # 经反向代理转发的请求看起来都来自 127.0.0.1，带转发头的一律不当作本机
        if self.headers.get("X-Forwarded-For") or self.headers.get("Forwarded") or self.headers.get("X-Real-IP"):
            return False
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _authorized(self):
        if not PASSCODE or self._is_host():
            return True
        if secrets.compare_digest(self._cookies.get("sfd_auth", ""), AUTH_TOKEN):
            return True
        given = self.headers.get("X-SFD-Passcode", "")
        return bool(given) and secrets.compare_digest(given, PASSCODE)

    def _gate(self, path):
        """口令校验；未通过时已经回了响应，返回 False。"""
        if self._authorized():
            return True
        if self.command in ("GET", "HEAD") and path in ("/", "/index.html"):
            self._send_html(LOGIN_PAGE.replace("__ERR__", ""), 401, head_only=self.command == "HEAD")
        else:
            self._send_json({"error": "需要访问口令"}, 401, head_only=self.command == "HEAD")
        return False

    def _handle_login(self):
        length = self._content_length()
        if length is None or length > 4096:
            self._send_html("<h1>400</h1>", 400)
            return
        params = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        given = params.get("passcode", [""])[0]
        if PASSCODE and secrets.compare_digest(given, PASSCODE):
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", f"sfd_auth={AUTH_TOKEN}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        time.sleep(1)  # 拖慢暴力猜测
        self._send_html(LOGIN_PAGE.replace("__ERR__", '<p class="err">口令不对，请重试</p>'), 401)

    def _content_length(self):
        """合法的 Content-Length（缺省按 0）；非法返回 None。"""
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        try:
            n = int(raw)
        except ValueError:
            return None
        return n if n >= 0 else None

    def _check_upload_length(self):
        """校验上传大小和磁盘剩余空间；不通过时已经回了响应，返回 None。"""
        length = self._content_length()
        if self.headers.get("Content-Length") is None:
            self._send_html("<h1>411 需要 Content-Length</h1>", 411)
            return None
        if length is None:
            self._send_html("<h1>400 Content-Length 不合法</h1>", 400)
            return None
        if length > MAX_UPLOAD_BYTES:
            self._send_html(f"<h1>413 文件超过上限 {html.escape(human_size(MAX_UPLOAD_BYTES))}</h1>", 413)
            return None
        try:
            if shutil.disk_usage(SHARE_DIR).free < length + 64 * 1024 * 1024:
                self._send_html("<h1>507 磁盘空间不足</h1>", 507)
                return None
        except OSError:
            pass
        return length

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
        self._touch_ok = True

    def _maybe_set_cookie(self):
        if getattr(self, "_new_cid", None):
            self.send_header("Set-Cookie", f"cid={self._new_cid}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax")

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
            disk_bytes = cached_dir_size(SHARE_DIR)
            host_controls = (
                '<button id="clearTexts" class="zh-btn ghost-btn" title="清空所有文字消息">清空聊天记录</button>'
                f'<span class="disk-note">共享文件占用 {html.escape(human_size(disk_bytes))}</span>'
            )
        page = (
            load_page_template()
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

    def _serve_static(self, url_path, head_only):
        _, content_type = _STATIC_ASSETS[url_path]
        try:
            _, etag, raw, gz = load_static(url_path)
        except OSError:
            self._send_html("<h1>404</h1>", 404, head_only=head_only)
            return
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        data = raw
        use_gzip = gz is not None and "gzip" in self.headers.get("Accept-Encoding", "")
        if use_gzip:
            data = gz
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")  # 每次带 ETag 校验，命中返回 304
        self.send_header("Vary", "Accept-Encoding")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        if not head_only:
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _touch_for_request(self, name):
        """播放视频会发很多 Range 请求；只有整文件或从头开始的请求才算一次"访问"。"""
        rng = self.headers.get("Range")
        if rng and not rng.startswith("bytes=0-"):
            return
        touch_file_access(name)

    def _feed_params(self, qs):
        filter_key = qs.get("filter", ["all"])[0]
        if filter_key not in ("all", "text", "media", "file"):
            filter_key = "all"
        try:
            limit = max(1, min(int(qs.get("limit", [FEED_PAGE_SIZE])[0]), 5000))
        except ValueError:
            limit = FEED_PAGE_SIZE
        try:
            since = float(qs.get("since", ["0"])[0])
        except ValueError:
            since = 0.0
        extra = {"query": qs.get("q", [""])[0][:200].strip(), "since": since, "sender": qs.get("sender", [""])[0][:80]}
        return filter_key, limit, extra

    def _handle_get(self, head_only):
        self._prepare()
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if not self._gate(path):
            return

        if path == "/" or path == "/index.html":
            self._render_page(head_only)
            return

        if path in _STATIC_ASSETS:
            self._serve_static(path, head_only)
            return

        if path == "/feed":
            filter_key, limit, extra = self._feed_params(qs)
            frag = build_feed(filter_key, self._client_id, self._is_host(), limit, **extra)
            self._send_html(frag, head_only=head_only)
            return

        if path == "/status":
            version = current_version()
            since = qs.get("since", [""])[0]
            if not head_only and since.isdigit() and int(since) == version:
                # 长轮询：没变化就挂起，有新内容立刻返回，不用每 3 秒空轮询
                version = wait_version_change(int(since), LONGPOLL_SECONDS)
                touch_presence(self._client_id)
            self._send_json({"online": online_count(), "version": version}, head_only=head_only)
            return

        if path == "/bundle":
            # 版本变化后一次拿齐 feed / 收藏 / 活跃发送者，省两次往返
            filter_key, limit, extra = self._feed_params(qs)
            is_host = self._is_host()
            self._send_json({
                "feed": build_feed(filter_key, self._client_id, is_host, limit, **extra),
                "favorites": build_favorites(self._client_id, is_host),
                "senders": get_active_senders(),
            }, head_only=head_only)
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
            self._touch_for_request(name)
            self._serve_file(name, head_only, inline=False)
            return

        if path.startswith("/view/"):
            name = os.path.basename(path[len("/view/"):])
            self._touch_for_request(name)
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

    def _send_file_body(self, fpath, start, length):
        """用 sendfile 直接把文件内容送进 socket，省掉用户态来回拷贝。"""
        if length <= 0:
            return
        try:
            with open(fpath, "rb") as f:
                self.connection.sendfile(f, offset=start, count=length)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    def _serve_file(self, name, head_only, inline=False):
        fpath = safe_share_path(name)
        if not fpath or not os.path.isfile(fpath):
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
                    end = min(int(e), size - 1) if e.strip() else size - 1
                else:
                    start = max(0, size - int(e))
                    end = size - 1
                if start > end or start >= size:
                    raise ValueError
                is_partial = True
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

        length = end - start + 1
        ext = os.path.splitext(name)[1].lower().lstrip(".")
        # 只有图片/音视频允许内联预览；HTML、SVG 等可执行脚本的类型一律当附件
        inline = inline and ext in INLINE_SAFE_EXTS
        self.send_response(206 if is_partial else 200)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self.send_header("Content-Type", mime)
        disposition = "inline" if inline else "attachment"
        self.send_header("Content-Disposition", f"{disposition}; filename*=UTF-8''" + urllib.parse.quote(name))
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if is_partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self._maybe_set_cookie()
        self.end_headers()

        if not head_only:
            self._send_file_body(fpath, start, length)

    # -- POST ------------------------------------------------------------
    def do_PUT(self):
        """Handle PUT requests (for file uploads in sync)"""
        self._prepare()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        if not self._gate(path):
            return

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
        if not self._gate(path):
            return

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
        if path == "/login":
            self._handle_login(); return
        if not self._gate(path):
            return

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
        length = self._content_length()
        if length is None or length > 256 * 1024:
            self._send_html("<h1>400 内容过大或长度不合法</h1>", 400)
            return
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

    def _read_upload(self):
        """校验并流式接收 multipart 上传；失败时已回响应，返回 None，否则返回 [UploadSink]。"""
        ctype = self.headers.get("Content-Type", "")
        m = _BOUNDARY_RE.search(ctype)
        if "multipart/form-data" not in ctype or not m:
            self._send_html("<h1>400 需要 multipart 表单</h1>", 400)
            return None
        length = self._check_upload_length()
        if length is None:
            return None
        try:
            return read_multipart_files(self.rfile, length, m.group(1) or m.group(2))
        except UploadError as e:
            self._send_html(f"<h1>{e.status} {html.escape(e.message)}</h1>", e.status)
        except (OSError, ValueError) as e:
            self.close_connection = True
            if not isinstance(e, (ConnectionError, TimeoutError)):
                self._send_html(f"<h1>500 上传失败: {html.escape(str(e))}</h1>", 500)
        return None

    def _handle_upload(self):
        sinks = self._read_upload()
        if sinks is None:
            return
        sender = self._nickname()
        try:
            while sinks:
                sink = sinks[0]
                add_file(sink.filename, sink.path, sink.size, sender, self._client_id)
                sinks.pop(0)
        except OSError as e:
            self._send_html(f"<h1>500 保存失败: {html.escape(str(e))}</h1>", 500)
            return
        finally:
            for sink in sinks:
                sink.discard()
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
        sinks = self._read_upload()
        if sinks is None:
            return
        try:
            if not sinks:
                self._send_html("<h1>400 未选择文件</h1>", 400)
                return
            first = sinks.pop(0)
            try:
                result = reshare_file(entry_id, first.path, first.size, self._nickname(), self._client_id)
            except OSError as e:
                first.discard()
                self._send_html(f"<h1>500 保存失败: {html.escape(str(e))}</h1>", 500)
                return
            if result is None:
                first.discard()
                self._send_html("<h1>404 记录不存在或未过期</h1>", 404)
                return
        finally:
            for sink in sinks:
                sink.discard()
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
                self._send_file_body(script_path, 0, size)
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
                self._send_file_body(file_path, 0, size)
        except (OSError, IOError):
            self._send_html("<h1>500</h1>", 500, head_only=head_only)

    def _handle_sync_create_folder(self):
        """POST /api/sync/folders - 创建新同步文件夹（主机专属）"""
        if not self._is_host():
            self._send_html("<h1>403</h1>", 403)
            return
        length = self._content_length()
        if length is None or length > 4096:
            self._send_json({"error": "Bad Content-Length"}, code=400)
            return
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

        length = self._content_length()
        if length is None or self.headers.get("Content-Length") is None:
            self._send_json({"error": "Missing or invalid Content-Length"}, code=400)
            return
        if length > MAX_UPLOAD_BYTES:
            self._send_json({"error": "File too large"}, code=413)
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

    def log_message(self, fmt, *args):
        # 长轮询和静态资源请求太频繁，不打印，避免刷屏和无谓的 IO
        if self.path.startswith(("/status", "/static/")):
            return
        sys.stderr.write("  %s\n" % (fmt % args))


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


def main():
    ip = get_lan_ip()
    server = ThreadingServer(("0.0.0.0", PORT), Handler)
    cleanup_stale_uploads()
    atexit.register(flush_files_if_dirty)  # 退出前把内存里的访问时间落盘
    threading.Thread(target=sweep_loop, daemon=True).start()
    print("=" * 46)
    print("  SendFileDrop 已启动")
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
