"""渲染：把文字/文件记录变成 HTML 列表项，以及页面模板加载。"""

import datetime
import html
import os
import urllib.parse
from config import BASE_DIR, FEED_PAGE_SIZE
from util import format_remaining, human_size
from store import load_files, load_texts, ttl_days_for_size


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
        f'<button class="zh-btn {cls}" data-kind="{kind}" data-key="{html.escape(item_id)}" title="收藏">'
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
            f'<button class="zh-btn del" data-kind="text" data-key="{tid}" title="删除这条文字" aria-label="删除这条文字">'
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
            '<path d="M4 7h16M9 7V4h6v3m3 0-1 13H7L6 7m4 4v5m4-5v5" stroke-linecap="round" stroke-linejoin="round"/>'
            '</svg></button>'
        )
    copy_btn = (
        f'<button class="zh-btn dl copy-text" data-content="{safe_content}" title="复制文字" aria-label="复制文字">'
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
            f'<button class="zh-btn del" data-kind="file" data-key="{eid}" title="删除 {safe_name}" aria-label="删除 {safe_name}">'
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
            '<path d="M4 7h16M9 7V4h6v3m3 0-1 13H7L6 7m4 4v5m4-5v5" stroke-linecap="round" stroke-linejoin="round"/>'
            '</svg></button>'
        )

    if purged:
        icon_html = _kind_icon(kind if kind == "file" else kind, ext_label)
        reshare_btn = (
            f'<button class="zh-btn dl reshare-btn" data-key="{eid}" title="重新分享" aria-label="重新分享">'
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
        f'<a class="zh-btn dl" href="{link}" download="{safe_name}" title="下载 {safe_name}" aria-label="下载 {safe_name}">'
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


def build_feed(filter_key, viewer_cid, is_host, limit=FEED_PAGE_SIZE, query="", since=0.0, sender=""):
    texts = load_texts()
    files = load_files()
    entries = [("text", t) for t in texts]
    for f in files:
        cat = "media" if f.get("kind") in ("image", "video", "audio") else "file"
        entries.append((cat, f))

    if filter_key != "all":
        entries = [e for e in entries if e[0] == filter_key]

    # 搜索 / 时间范围 / 发送者筛选放在服务端做，分页后也能覆盖全部记录
    if query:
        q = query.lower()
        entries = [e for e in entries if q in (e[1].get("content") if e[0] == "text" else e[1].get("name", "")).lower()]
    if since:
        entries = [e for e in entries if e[1].get("ts", e[1].get("uploaded_ts", 0)) >= since]
    if sender:
        entries = [e for e in entries if (e[1].get("sender") or "访客") == sender]

    if filter_key in ("media", "file"):
        # 文件/多媒体是"浏览列表"：最近被碰过的排前面，长期没用的下沉到底部
        entries.sort(key=lambda e: e[1].get("last_seen_ts", e[1].get("uploaded_ts", 0)), reverse=True)
    else:
        # 全部/文字是"聊天时间流"：旧的在上，新的在下，跟聊天软件一致
        entries.sort(key=lambda e: e[1].get("ts", e[1].get("uploaded_ts", 0)))

    # 只渲染最近的一页：聊天流取末尾，浏览列表取开头；其余用"加载更多"按钮按需取
    hidden = max(0, len(entries) - limit)
    if hidden:
        entries = entries[-limit:] if filter_key in ("all", "text") else entries[:limit]

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
        if query or since or sender:
            title, sub = "没有符合条件的内容", "换个关键词、时间范围或发送者试试"
        return _empty_block(title, sub)
    more = ""
    if hidden:
        more = (
            f'<div class="feed-more"><button class="zh-btn" type="button" data-load-more>'
            f'加载更多（还有 {hidden} 条）</button></div>'
        )
    if filter_key in ("all", "text"):
        return more + "<ul>" + "".join(rows) + "</ul>"
    return "<ul>" + "".join(rows) + "</ul>" + more


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


_template_cache = [None, ""]  # [stat 签名, 内容]


def load_page_template():
    path = os.path.join(BASE_DIR, "templates", "index.html")
    st = os.stat(path)
    sig = (st.st_mtime_ns, st.st_size)
    if _template_cache[0] != sig:
        with open(path, "r", encoding="utf-8") as f:
            _template_cache[1] = f.read()
        _template_cache[0] = sig
    return _template_cache[1]
