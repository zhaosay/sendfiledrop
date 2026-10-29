"""通用小工具：二维码、体积/时间格式化、局域网 IP、目录大小、版本号。"""

import functools
import json
import os
import socket
import sys
import time
from config import BASE_DIR


sys.path.insert(0, BASE_DIR)


try:
    import qr as _qr
except Exception:  # noqa: BLE001
    _qr = None


@functools.lru_cache(maxsize=8)
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


_ip_cache = [0.0, "127.0.0.1"]


def get_lan_ip():
    """获取本机在局域网中的 IP 地址（缓存 30 秒，避免每次渲染都新建 socket）"""
    now = time.time()
    if now - _ip_cache[0] < 30:
        return _ip_cache[1]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    _ip_cache[0], _ip_cache[1] = now, ip
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


_dir_size_cache = [0.0, 0]


def cached_dir_size(path, ttl=10):
    now = time.time()
    if now - _dir_size_cache[0] >= ttl:
        _dir_size_cache[0], _dir_size_cache[1] = now, _dir_size(path)
    return _dir_size_cache[1]


@functools.lru_cache(maxsize=1)
def load_version():
    try:
        with open(os.path.join(BASE_DIR, "version.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("version", "unknown")
    except (OSError, ValueError):
        return "unknown"
