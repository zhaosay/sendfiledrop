"""流式 multipart 上传解析：边读边写临时文件，内存占用与文件大小无关。"""

import os
import re
import urllib.parse
import uuid
from config import SHARE_DIR


class UploadError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class UploadSink:
    """一个上传文件对应的临时文件，写满后由 add_file/reshare_file 原子地改名成正式文件。"""

    def __init__(self, filename):
        self.filename = filename
        self.path = os.path.join(SHARE_DIR, ".upload-" + uuid.uuid4().hex + ".part")
        self.size = 0
        self._f = open(self.path, "wb")

    def write(self, data):
        self._f.write(data)
        self.size += len(data)

    def close(self):
        if not self._f.closed:
            self._f.close()

    def discard(self):
        self.close()
        try:
            os.remove(self.path)
        except OSError:
            pass


_DISP_FILENAME_STAR = re.compile(r"filename\*\s*=\s*([^;]+)", re.I)


_DISP_FILENAME_QUOTED = re.compile(r'filename\s*=\s*"((?:[^"\\]|\\.)*)"', re.I)


_DISP_FILENAME_BARE = re.compile(r"filename\s*=\s*([^;\s]+)", re.I)


def _filename_from_headers(raw_headers):
    """从 part 头里取文件名；没有 filename 字段（普通表单项）或为空返回 None。"""
    text = raw_headers.decode("utf-8", "replace")
    for line in text.split("\r\n"):
        if not line.lower().startswith("content-disposition"):
            continue
        m = _DISP_FILENAME_STAR.search(line)
        if m:
            val = m.group(1).strip().strip('"')
            if "''" in val:
                return urllib.parse.unquote(val.split("''", 1)[1]) or None
        m = _DISP_FILENAME_QUOTED.search(line)
        if m:
            return m.group(1).replace('\\"', '"').replace("\\\\", "\\") or None
        m = _DISP_FILENAME_BARE.search(line)
        if m:
            return m.group(1) or None
    return None


def read_multipart_files(rfile, length, boundary):
    """流式解析 multipart/form-data，返回 [UploadSink, ...]（普通表单项被忽略）。
    出错时已创建的临时文件会被清理；成功后会把请求体剩余部分读完，保证长连接不错位。"""
    delim = b"\r\n--" + boundary.encode("latin-1")
    keep = len(delim) + 4          # 缓冲尾部留一段，避免分隔符恰好被切在两次读取之间
    remaining = length
    buf = b"\r\n"                  # 第一个分隔符前没有 CRLF，补一个统一处理
    sinks = []
    current = None

    def fill():
        nonlocal buf, remaining
        chunk = rfile.read(min(65536, remaining)) if remaining > 0 else b""
        if not chunk:
            raise UploadError(400, "上传数据不完整")
        remaining -= len(chunk)
        buf += chunk

    try:
        state = "preamble"
        while True:
            if state == "preamble":
                i = buf.find(delim)
                if i < 0:
                    buf = buf[-keep:]
                    fill()
                    continue
                buf = buf[i + len(delim):]
                state = "after"
            elif state == "after":
                while len(buf) < 2:
                    fill()
                if buf[:2] == b"--":
                    break
                j = buf.find(b"\r\n")
                while j < 0:
                    if len(buf) > 1024:
                        raise UploadError(400, "上传格式错误")
                    fill()
                    j = buf.find(b"\r\n")
                buf = buf[j + 2:]
                state = "headers"
            elif state == "headers":
                j = buf.find(b"\r\n\r\n")
                while j < 0:
                    if len(buf) > 65536:
                        raise UploadError(400, "上传头部过大")
                    fill()
                    j = buf.find(b"\r\n\r\n")
                filename = _filename_from_headers(buf[:j])
                buf = buf[j + 4:]
                current = UploadSink(filename) if filename else None
                state = "body"
            else:  # body
                i = buf.find(delim)
                if i >= 0:
                    if current:
                        current.write(buf[:i])
                        current.close()
                        sinks.append(current)
                        current = None
                    buf = buf[i + len(delim):]
                    state = "after"
                    continue
                if len(buf) > keep:
                    cut = len(buf) - keep
                    if current:
                        current.write(buf[:cut])
                    buf = buf[cut:]
                fill()
        while remaining > 0:  # 读掉结尾分隔符之后的多余数据
            chunk = rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
    except BaseException:
        if current:
            current.discard()
        for sink in sinks:
            sink.discard()
        raise
    return sinks
