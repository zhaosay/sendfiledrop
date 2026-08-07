#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sendfiledrop 局域网文件传输服务器 (零依赖，仅用 Python 标准库)

用法:
    python3 server.py            # 默认端口 8000
    python3 server.py 9000       # 指定端口

启动后，同一 WiFi / 局域网下的任何设备(手机、平板、别的电脑)
用浏览器打开显示的地址即可上传 / 下载文件。

上传的文件保存在本脚本同目录下的 "shared" 文件夹里。
"""

import os
import sys
import html
import socket
import urllib.parse
import http.server
import socketserver
import datetime
import mimetypes

# ---- 配置 ----
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SHARE_DIR = os.path.join(BASE_DIR, "shared")
os.makedirs(SHARE_DIR, exist_ok=True)

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
        return _qr.matrix_to_svg(_qr.generate_matrix(url), box=6, border=3)
    except Exception:  # noqa: BLE001
        return ""


def human_size(n):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


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


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>sendfiledrop · 局域网文件传输</title>
<style>
  :root {{ --ink:#172033; --muted:#6f7b91; --line:#e6eaf0; --card:rgba(255,255,255,.88);
    --blue:#2667ff; --blue2:#5b8cff; --green:#14b87a; --danger:#e5484d; }}
  * {{ box-sizing:border-box; }}
  html {{ min-height:100%; background:#f4f7fb; }}
  body {{ min-height:100%; margin:0; color:var(--ink); font-family:-apple-system,BlinkMacSystemFont,
    "SF Pro Display","PingFang SC","Microsoft YaHei",sans-serif; -webkit-font-smoothing:antialiased;
    background:radial-gradient(circle at 8% 0%,rgba(82,130,255,.17),transparent 28rem),
      radial-gradient(circle at 95% 16%,rgba(36,201,154,.12),transparent 25rem),#f4f7fb; }}
  button,a,.drop {{ -webkit-tap-highlight-color:transparent; }}
  button {{ font:inherit; }}
  .wrap {{ width:min(1040px,100%); margin:0 auto; padding:34px 24px 72px; }}
  .topbar {{ display:flex; align-items:center; justify-content:space-between; margin-bottom:32px; }}
  .brand {{ display:flex; align-items:center; gap:12px; font-size:18px; font-weight:750; letter-spacing:-.02em; }}
  .logo {{ display:grid; place-items:center; width:40px; height:40px; color:#fff; border-radius:13px;
    background:linear-gradient(145deg,var(--blue2),var(--blue)); box-shadow:0 9px 22px rgba(38,103,255,.27); }}
  .logo svg {{ width:23px; }}
  .status {{ display:flex; align-items:center; gap:8px; color:#4b5970; font-size:13px; font-weight:600;
    padding:8px 12px; border:1px solid rgba(255,255,255,.8); background:rgba(255,255,255,.6);
    border-radius:99px; box-shadow:0 3px 14px rgba(36,49,73,.05); backdrop-filter:blur(10px); }}
  .dot {{ width:8px; height:8px; border-radius:50%; background:var(--green); box-shadow:0 0 0 4px rgba(20,184,122,.12); }}
  .hero {{ display:grid; grid-template-columns:minmax(0,1.2fr) minmax(280px,.8fr); gap:22px; margin-bottom:22px; }}
  .intro {{ padding:26px 4px 26px 0; align-self:center; }}
  .eyebrow {{ color:var(--blue); font-size:13px; font-weight:750; letter-spacing:.08em; text-transform:uppercase; }}
  h1 {{ margin:10px 0 12px; font-size:clamp(34px,5vw,52px); line-height:1.08; letter-spacing:-.05em; }}
  .lead {{ max-width:560px; margin:0; color:var(--muted); font-size:16px; line-height:1.8; }}
  .badges {{ display:flex; flex-wrap:wrap; gap:9px; margin-top:22px; }}
  .badge {{ color:#59657a; font-size:12px; font-weight:600; padding:7px 10px; border:1px solid rgba(215,222,233,.85);
    border-radius:9px; background:rgba(255,255,255,.55); }}
  .card {{ background:var(--card); border:1px solid rgba(255,255,255,.9); border-radius:22px;
    box-shadow:0 18px 50px rgba(35,52,79,.08),0 2px 8px rgba(35,52,79,.04); backdrop-filter:blur(16px); }}
  .connect {{ padding:22px; }}
  .card-label {{ color:var(--muted); font-size:12px; font-weight:700; letter-spacing:.06em; text-transform:uppercase; }}
  .addr {{ display:flex; align-items:center; gap:18px; margin-top:16px; }}
  .qr {{ flex:0 0 auto; padding:9px; line-height:0; background:#fff; border:1px solid var(--line); border-radius:16px;
    box-shadow:0 5px 16px rgba(31,46,69,.08); }}
  .qr svg {{ width:124px; height:124px; border-radius:7px; }}
  .info {{ min-width:0; }}
  .info-title {{ font-weight:700; font-size:15px; margin-bottom:6px; }}
  .url {{ color:var(--blue); font-size:15px; font-weight:650; line-height:1.45; word-break:break-all; }}
  .hint {{ color:var(--muted); font-size:12px; line-height:1.5; margin-top:6px; }}
  .copy {{ display:inline-flex; align-items:center; justify-content:center; gap:7px; margin-top:13px; padding:9px 13px;
    color:#40506b; font-size:13px; font-weight:650; background:#f0f3f8; border:0; border-radius:10px; cursor:pointer; transition:.18s; }}
  .copy:hover {{ color:var(--blue); background:#e8efff; transform:translateY(-1px); }}
  .copy svg {{ width:15px; }}
  .workspace {{ display:grid; grid-template-columns:minmax(0,.85fr) minmax(0,1.15fr); gap:22px; align-items:start; }}
  .upload-card,.files-card {{ padding:24px; }}
  .section-head {{ display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:18px; }}
  .section-title {{ display:flex; align-items:center; gap:10px; margin:0; font-size:17px; letter-spacing:-.02em; }}
  .section-icon {{ display:grid; place-items:center; width:34px; height:34px; color:var(--blue); background:#edf3ff; border-radius:10px; }}
  .section-icon svg {{ width:18px; }}
  .section-note {{ color:var(--muted); font-size:12px; }}
  .drop {{ display:flex; min-height:260px; padding:30px 18px; flex-direction:column; align-items:center; justify-content:center;
    text-align:center; border:1.5px dashed #bdc8d9; border-radius:17px; cursor:pointer; background:linear-gradient(145deg,#fafcff,#f5f8fc);
    transition:border-color .2s,background .2s,transform .2s,box-shadow .2s; }}
  .drop:hover,.drop.drag {{ border-color:var(--blue2); background:#f1f6ff; box-shadow:inset 0 0 0 1px rgba(38,103,255,.05); }}
  .drop.drag {{ transform:scale(1.01); }}
  .upload-mark {{ display:grid; place-items:center; width:58px; height:58px; color:var(--blue); background:#fff; border-radius:18px;
    box-shadow:0 8px 24px rgba(38,103,255,.14); margin-bottom:17px; }}
  .upload-mark svg {{ width:27px; }}
  .drop strong {{ font-size:15px; }}
  .drop-copy {{ color:var(--muted); font-size:12px; line-height:1.6; margin-top:7px; }}
  .browse {{ color:var(--blue); font-weight:700; }}
  .picked {{ min-height:20px; color:var(--blue); font-size:12px; font-weight:650; margin-top:11px; }}
  .btn {{ width:100%; margin-top:14px; padding:13px 18px; color:#fff; font-weight:700; border:0; border-radius:12px; cursor:pointer;
    background:linear-gradient(135deg,var(--blue2),var(--blue)); box-shadow:0 9px 20px rgba(38,103,255,.2); transition:.18s; }}
  .btn:hover:not(:disabled) {{ transform:translateY(-1px); box-shadow:0 12px 24px rgba(38,103,255,.27); }}
  .btn:disabled {{ color:#9ca7b8; background:#e8ecf2; box-shadow:none; cursor:not-allowed; }}
  #queue {{ margin-top:14px; }}
  .qitem {{ padding:11px 2px; border-top:1px solid var(--line); font-size:12px; }}
  .qitem .top {{ display:flex; justify-content:space-between; gap:12px; }}
  .qitem .name {{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .qitem .state {{ color:var(--muted); white-space:nowrap; }}
  .qitem.done .state {{ color:var(--green); }} .qitem.err .state {{ color:var(--danger); }}
  .qbar {{ height:5px; margin-top:7px; overflow:hidden; background:#e7ebf1; border-radius:99px; }}
  .qbar div {{ width:0; height:100%; background:linear-gradient(90deg,var(--blue2),var(--blue)); border-radius:99px; transition:width .15s; }}
  ul {{ list-style:none; padding:0; margin:0; }}
  li {{ display:flex; align-items:center; gap:12px; padding:14px 4px; border-bottom:1px solid var(--line); }}
  li:last-child {{ border-bottom:0; }}
  .file-icon {{ display:grid; place-items:center; flex:0 0 auto; width:42px; height:42px; color:#62708a; background:#f0f3f8; border-radius:12px; }}
  .file-icon svg {{ width:20px; }}
  .fmeta {{ flex:1; min-width:0; }}
  .fname {{ overflow:hidden; color:#263249; font-size:14px; font-weight:650; text-overflow:ellipsis; white-space:nowrap; }}
  .meta {{ color:var(--muted); font-size:11px; margin-top:4px; }}
  .actions {{ display:flex; gap:7px; flex:0 0 auto; }}
  .dl,.del {{ display:grid; place-items:center; width:36px; height:36px; padding:0; border:0; border-radius:10px; cursor:pointer; transition:.18s; }}
  .dl {{ color:var(--blue); background:#edf3ff; text-decoration:none; }}
  .del {{ color:#a16a6d; background:#f7f1f2; }}
  .dl:hover {{ color:#fff; background:var(--blue); }} .del:hover {{ color:#fff; background:var(--danger); }}
  .dl svg,.del svg {{ width:17px; }}
  .empty {{ display:grid; place-items:center; min-height:260px; color:var(--muted); text-align:center; font-size:13px; }}
  .empty svg {{ width:42px; color:#b3bdcb; margin-bottom:10px; }}
  .footer {{ margin-top:24px; color:#8993a4; text-align:center; font-size:11px; }}
  :focus-visible {{ outline:3px solid rgba(38,103,255,.25); outline-offset:2px; }}
  @media (max-width:800px) {{ .hero,.workspace {{ grid-template-columns:1fr; }} .intro {{ padding:8px 2px 4px; }} }}
  @media (max-width:560px) {{
    .wrap {{ padding:20px 14px 48px; }} .topbar {{ margin-bottom:24px; }} .status {{ font-size:12px; }}
    h1 {{ font-size:36px; }} .lead {{ font-size:14px; }} .badges {{ margin-top:17px; }}
    .card {{ border-radius:18px; }} .connect,.upload-card,.files-card {{ padding:18px; }}
    .addr {{ align-items:flex-start; }} .qr svg {{ width:96px; height:96px; }} .info-title {{ font-size:14px; }}
    .url {{ font-size:13px; }} .drop {{ min-height:220px; }} li {{ gap:10px; padding:13px 0; }}
    .file-icon {{ width:38px; height:38px; }} .dl,.del {{ width:34px; height:34px; }}
  }}
  @media (prefers-reduced-motion:reduce) {{ * {{ scroll-behavior:auto!important; transition:none!important; }} }}
</style>
</head>
<body>
<div class="wrap">
  <header class="topbar">
    <div class="brand"><span class="logo"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12.6a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0M12 20h.01M2 9a14.4 14.4 0 0 1 20 0" stroke-linecap="round"/></svg></span>sendfiledrop</div>
    <div class="status"><span class="dot"></span>服务运行中</div>
  </header>

  <section class="hero">
    <div class="intro">
      <div class="eyebrow">Local file sharing</div>
      <h1>文件传输，<br>近在咫尺。</h1>
      <p class="lead">无需数据线或云盘，在同一 WiFi 下即可安全、快速地跨设备分享文件。</p>
      <div class="badges"><span class="badge">局域网直传</span><span class="badge">无需登录</span><span class="badge">数据不出本地</span></div>
    </div>
    <div class="card connect">
      <div class="card-label">连接到此设备</div>
      <div class="addr">
        <div class="qr">{qr}</div>
        <div class="info">
          <div class="info-title">扫码立即访问</div>
          <div class="url" id="url">{url}</div>
          <div class="hint">请确保设备连接到同一个 WiFi</div>
          <button class="copy" id="copy"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M15 9V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h3"/></svg><span>复制地址</span></button>
        </div>
      </div>
    </div>
  </section>

  <main class="workspace">
    <section class="card upload-card">
      <div class="section-head"><h2 class="section-title"><span class="section-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 16V4m0 0L7 9m5-5 5 5M5 14v4a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-4" stroke-linecap="round" stroke-linejoin="round"/></svg></span>上传文件</h2></div>
      <div id="drop" class="drop" role="button" tabindex="0">
        <span class="upload-mark"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 16V5m0 0L8 9m4-4 4 4M5 15v3a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-3" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
        <strong>拖放文件到这里</strong>
        <div class="drop-copy">或 <span class="browse">浏览设备文件</span><br>支持多选，将按顺序上传</div>
        <input id="file" type="file" multiple style="display:none">
        <div id="picked" class="picked"></div>
      </div>
      <button id="send" class="btn" disabled>选择文件后上传</button>
      <div id="queue"></div>
    </section>

    <section class="card files-card">
      <div class="section-head"><h2 class="section-title"><span class="section-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z" stroke-linejoin="round"/></svg></span>共享文件</h2><span class="section-note">所有设备可下载</span></div>
      {filelist}
    </section>
  </main>
  <div class="footer">sendfiledrop · 仅在当前局域网内传输</div>
</div>

<script>
  var drop = document.getElementById('drop');
  var file = document.getElementById('file');
  var send = document.getElementById('send');
  var picked = document.getElementById('picked');
  var queue = document.getElementById('queue');
  var copyBtn = document.getElementById('copy');

  copyBtn.onclick = function() {{
    var t = document.getElementById('url').textContent;
    if (navigator.clipboard) navigator.clipboard.writeText(t);
    copyBtn.querySelector('span').textContent = '已复制';
    setTimeout(function(){{ copyBtn.querySelector('span').textContent = '复制地址'; }}, 1500);
  }};

  drop.onclick = function() {{ file.click(); }};
  drop.onkeydown = function(e) {{ if (e.key === 'Enter' || e.key === ' ') {{ e.preventDefault(); file.click(); }} }};
  file.onchange = function() {{ showPicked(file.files); }};

  ['dragenter','dragover'].forEach(function(e){{
    drop.addEventListener(e, function(ev){{ ev.preventDefault(); drop.classList.add('drag'); }});
  }});
  ['dragleave','drop'].forEach(function(e){{
    drop.addEventListener(e, function(ev){{ ev.preventDefault(); drop.classList.remove('drag'); }});
  }});
  drop.addEventListener('drop', function(ev){{
    file.files = ev.dataTransfer.files;
    showPicked(file.files);
  }});

  function showPicked(files) {{
    if (!files.length) {{ picked.textContent=''; send.textContent='选择文件后上传'; send.disabled=true; return; }}
    picked.textContent = '已选择 ' + files.length + ' 个文件';
    send.textContent = '开始上传 ' + files.length + ' 个文件';
    send.disabled = false;
  }}

  // 逐个上传：一个传完再传下一个
  send.onclick = function() {{
    var files = Array.prototype.slice.call(file.files);
    if (!files.length) return;
    send.disabled = true;
    drop.style.pointerEvents = 'none';
    queue.innerHTML = '';

    var rows = files.map(function(f) {{
      var el = document.createElement('div');
      el.className = 'qitem';
      el.innerHTML = '<div class="top"><span class="name"></span>' +
                     '<span class="state">等待中</span></div>' +
                     '<div class="qbar"><div></div></div>';
      el.querySelector('.name').textContent = f.name;
      queue.appendChild(el);
      return el;
    }});

    var i = 0;
    function next() {{
      if (i >= files.length) {{
        setTimeout(function(){{ location.reload(); }}, 600);
        return;
      }}
      uploadOne(files[i], rows[i], function() {{ i++; next(); }});
    }}
    next();
  }};

  function uploadOne(f, row, done) {{
    var state = row.querySelector('.state');
    var barIn = row.querySelector('.qbar div');
    state.textContent = '上传中…';
    var fd = new FormData();
    fd.append('file', f);
    var xhr = new XMLHttpRequest();
    xhr.open('POST', '/upload');
    xhr.upload.onprogress = function(e) {{
      if (e.lengthComputable) {{
        var p = Math.round(e.loaded / e.total * 100);
        barIn.style.width = p + '%';
        state.textContent = p + '%';
      }}
    }};
    xhr.onload = function() {{
      if (xhr.status >= 200 && xhr.status < 400) {{
        row.classList.add('done'); barIn.style.width = '100%'; state.textContent = '完成';
      }} else {{
        row.classList.add('err'); state.textContent = '失败';
      }}
      done();
    }};
    xhr.onerror = function() {{ row.classList.add('err'); state.textContent = '失败'; done(); }};
    xhr.send(fd);
  }}

  // 删除已上传的文件
  document.querySelectorAll('.del').forEach(function(btn) {{
    btn.onclick = function() {{
      var name = btn.getAttribute('data-name');
      if (!confirm('确定删除 "' + name + '" 吗？')) return;
      btn.disabled = true;
      fetch('/delete/' + encodeURIComponent(name), {{ method: 'POST' }})
        .then(function(res) {{
          if (res.ok) {{ location.reload(); }}
          else {{ alert('删除失败'); btn.disabled = false; }}
        }})
        .catch(function() {{ alert('删除失败'); btn.disabled = false; }});
    }};
  }});
</script>
</body>
</html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def _send_html(self, body, code=200, head_only=False):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
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

    def build_filelist(self):
        try:
            names = sorted(os.listdir(SHARE_DIR))
        except OSError:
            names = []
        rows = []
        for name in names:
            path = os.path.join(SHARE_DIR, name)
            if not os.path.isfile(path):
                continue
            st = os.stat(path)
            when = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
            link = "/download/" + urllib.parse.quote(name)
            safe_name = html.escape(name)
            ext = os.path.splitext(name)[1].lower().lstrip(".") or "file"
            ext_label = html.escape(ext[:5].upper())
            rows.append(
                f'<li>'
                f'<div class="file-icon" title="{ext_label} 文件"><span style="font-size:10px;font-weight:800;letter-spacing:-.03em">{ext_label}</span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" style="display:none">'
                f'<path d="M7 3h7l4 4v14H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z"/>'
                f'<path d="M14 3v5h5"/></svg></div>'
                f'<div class="fmeta"><div class="fname">{safe_name}</div>'
                f'<div class="meta">{human_size(st.st_size)} · {when}</div></div>'
                f'<div class="actions">'
                f'<a class="dl" href="{link}" download="{safe_name}" title="下载 {safe_name}" aria-label="下载 {safe_name}">'
                f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 4v11m0 0 4-4m-4 4-4-4M5 20h14" stroke-linecap="round" stroke-linejoin="round"/></svg></a>'
                f'<button class="del" data-name="{safe_name}" title="删除 {safe_name}" aria-label="删除 {safe_name}">'
                f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 7h16M9 7V4h6v3m3 0-1 13H7L6 7m4 4v5m4-5v5" stroke-linecap="round" stroke-linejoin="round"/></svg></button>'
                f'</div>'
                f'</li>'
            )
        if not rows:
            return ('<div class="empty"><div><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">'
                    '<path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/>'
                    '</svg><br>暂无共享文件<br><span style="font-size:11px">上传后会显示在这里</span></div></div>')
        return "<ul>" + "".join(rows) + "</ul>"

    def do_GET(self):
        self._handle_get(head_only=False)

    def do_HEAD(self):
        self._handle_get(head_only=True)

    def _handle_get(self, head_only):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)

        if path == "/" or path == "/index.html":
            ip = get_lan_ip()
            url = f"http://{ip}:{PORT}"
            page = PAGE_TEMPLATE.format(
                url=url, qr=qr_svg_for(url), filelist=self.build_filelist()
            )
            self._send_html(page, head_only=head_only)
            return

        if path.startswith("/download/"):
            self._serve_file(os.path.basename(path[len("/download/"):]), head_only)
            return

        self._send_html("<h1>404</h1>", 404, head_only=head_only)

    def _serve_file(self, name, head_only):
        fpath = os.path.join(SHARE_DIR, name)
        if not os.path.isfile(fpath):
            self._send_html("<h1>404 文件不存在</h1>", 404, head_only=head_only)
            return

        size = os.path.getsize(fpath)
        start, end = 0, size - 1

        # 断点续传 / 多线程下载 (Range 请求)
        rng = self.headers.get("Range")
        is_partial = False
        if rng and rng.startswith("bytes="):
            try:
                s, _, e = rng[len("bytes="):].partition("-")
                if s.strip():
                    start = int(s)
                    end = int(e) if e.strip() else size - 1
                else:  # bytes=-N (最后 N 字节)
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
        self.send_header(
            "Content-Disposition",
            "attachment; filename*=UTF-8''" + urllib.parse.quote(name),
        )
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if is_partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
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
            # 客户端中途断开(常见于安卓下载器/预览)，忽略即可
            pass

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path.startswith("/delete/"):
            self._handle_delete(parsed.path[len("/delete/"):])
            return

        if parsed.path != "/upload":
            self._send_html("<h1>404</h1>", 404)
            return

        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype or "boundary=" not in ctype:
            self._send_html("<h1>400 需要 multipart 表单</h1>", 400)
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        boundary = ctype.split("boundary=", 1)[1].strip().strip('"')
        try:
            saved = self._parse_multipart(body, boundary)
        except Exception as e:  # noqa: BLE001
            self._send_html(f"<h1>500 上传解析失败: {html.escape(str(e))}</h1>", 500)
            return

        _ = saved
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def _handle_delete(self, raw_name):
        name = os.path.basename(urllib.parse.unquote(raw_name))
        fpath = os.path.join(SHARE_DIR, name)
        if not name or not os.path.isfile(fpath):
            self._send_html("<h1>404 文件不存在</h1>", 404)
            return
        try:
            os.remove(fpath)
        except OSError as e:  # noqa: BLE001
            self._send_html(f"<h1>500 删除失败: {html.escape(str(e))}</h1>", 500)
            return
        self._send_html("ok")

    def _parse_multipart(self, body, boundary):
        """轻量 multipart 解析，不依赖已被移除的 cgi 模块。"""
        delim = b"--" + boundary.encode("latin-1")
        parts = body.split(delim)
        saved = 0
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
            dest = os.path.join(SHARE_DIR, safe)
            base, ext = os.path.splitext(safe)
            i = 1
            while os.path.exists(dest):
                dest = os.path.join(SHARE_DIR, f"{base}({i}){ext}")
                i += 1
            with open(dest, "wb") as out:
                out.write(content)
            saved += 1
        return saved

    def log_message(self, fmt, *args):
        # 精简日志
        sys.stderr.write("  %s\n" % (fmt % args))


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ip = get_lan_ip()
    server = ThreadingServer(("0.0.0.0", PORT), Handler)
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
