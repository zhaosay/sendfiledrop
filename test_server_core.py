"""核心 HTTP 行为测试：流式上传、Range 下载、缓存、长轮询、分页、安全头。

在临时数据目录里启动一个真实的 server.py 子进程，不会碰用户的 shared/ 和 *.json。
"""

import gzip
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAX_UPLOAD = 6 * 1024 * 1024


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def multipart(parts, boundary="----sfdTestBoundary"):
    """parts: [(filename, bytes)]；返回 (content_type, body)。"""
    out = b""
    for filename, content in parts:
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode("utf-8")
        out += content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", out


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.port = free_port()
        env = dict(os.environ, SFD_DATA_DIR=cls.tmp.name, SFD_MAX_UPLOAD_BYTES=str(MAX_UPLOAD))
        cls.proc = subprocess.Popen(
            [sys.executable, "-I", str(ROOT / "server.py"), str(cls.port)],
            cwd=cls.tmp.name, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)
        cls.tmp.cleanup()

    # -- helpers --
    def conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)

    def request(self, method, path, body=None, headers=None, conn=None):
        c = conn or self.conn()
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        if conn is None:
            c.close()
        return r, data

    def upload(self, parts, path="/upload"):
        ctype, body = multipart(parts)
        return self.request("POST", path, body, {"Content-Type": ctype, "X-Nickname": "tester"})

    def files(self):
        return json.loads((Path(self.tmp.name) / "files.json").read_text())

    # -- tests --
    def test_upload_stream_roundtrip_and_boundary_lookalike(self):
        # 内容里夹带类似分隔符的字节，且跨越 64KB 读取边界
        payload = os.urandom(70000) + b"\r\n------sfdTestBoundar" + os.urandom(70000) + b"\r\n--"
        r, _ = self.upload([("rt.bin", payload)])
        self.assertEqual(r.status, 200)
        r, data = self.request("GET", "/download/rt.bin")
        self.assertEqual(data, payload)
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")

    def test_large_upload_and_no_part_leftovers(self):
        payload = os.urandom(4 * 1024 * 1024)
        r, _ = self.upload([("big.bin", payload)])
        self.assertEqual(r.status, 200)
        _, data = self.request("GET", "/download/big.bin")
        self.assertEqual(data, payload)
        leftovers = [n for n in os.listdir(Path(self.tmp.name) / "shared") if n.endswith(".part")]
        self.assertEqual(leftovers, [])

    def test_multiple_files_and_unicode_name(self):
        r, _ = self.upload([("你好 文件.txt", b"hello"), ("second.txt", b"world")])
        self.assertEqual(r.status, 200)
        _, data = self.request("GET", "/download/" + urllib.parse.quote("你好 文件.txt"))
        self.assertEqual(data, b"hello")

    def test_hostile_filenames_are_sanitized(self):
        r, _ = self.upload([("../../etc/evil.txt", b"x"), (".hidden", b"y")])
        self.assertEqual(r.status, 200)
        names = {f["name"] for f in self.files()}
        self.assertIn("evil.txt", names)
        self.assertIn("hidden", names)  # 开头的点被去掉，不会变成隐藏文件
        self.assertFalse((Path(self.tmp.name) / "etc").exists())
        r, _ = self.request("GET", "/download/.upload-x.part")
        self.assertEqual(r.status, 404)
        r, _ = self.request("GET", "/download/..%2f..%2fserver.py")
        self.assertEqual(r.status, 404)

    def test_concurrent_same_name_uploads_never_overwrite(self):
        results = []

        def go(i):
            r, _ = self.upload([("same.txt", f"content-{i}".encode())])
            results.append(r.status)

        threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(results, [200] * 8)
        names = [f["name"] for f in self.files() if f["name"].startswith("same")]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(names), 8)
        bodies = set()
        for n in names:
            _, data = self.request("GET", "/download/" + urllib.parse.quote(n))
            bodies.add(data)
        self.assertEqual(len(bodies), 8)

    def test_oversize_upload_rejected_before_reading(self):
        # 服务端先回 413 再断开；客户端还在发数据时可能直接收到 RST，两种结果都算拒绝成功
        try:
            r, _ = self.upload([("huge.bin", b"0" * (MAX_UPLOAD + 10))])
            self.assertEqual(r.status, 413)
        except (ConnectionError, http.client.HTTPException):
            pass
        self.assertNotIn("huge.bin", {f["name"] for f in self.files()})

    def test_truncated_upload_cleans_up(self):
        ctype, body = multipart([("trunc.bin", b"a" * 200000)])
        c = self.conn()
        c.putrequest("POST", "/upload")
        c.putheader("Content-Type", ctype)
        c.putheader("Content-Length", str(len(body)))
        c.endheaders()
        c.send(body[:100000])  # 只发一半就断开
        c.close()
        time.sleep(1)
        shared = os.listdir(Path(self.tmp.name) / "shared")
        self.assertFalse([n for n in shared if n.endswith(".part")])
        self.assertNotIn("trunc.bin", {f["name"] for f in self.files()})

    def test_range_download(self):
        data = bytes(range(256)) * 100
        self.upload([("range.bin", data)])
        r, part = self.request("GET", "/download/range.bin", headers={"Range": "bytes=100-199"})
        self.assertEqual(r.status, 206)
        self.assertEqual(part, data[100:200])
        self.assertEqual(r.getheader("Content-Range"), f"bytes 100-199/{len(data)}")
        r, part = self.request("GET", "/download/range.bin", headers={"Range": "bytes=-50"})
        self.assertEqual(part, data[-50:])
        r, _ = self.request("GET", "/download/range.bin", headers={"Range": f"bytes={len(data) + 5}-"})
        self.assertEqual(r.status, 416)
        r, part = self.request("GET", "/download/range.bin", headers={"Range": f"bytes=0-{len(data) * 2}"})
        self.assertEqual(r.status, 206)
        self.assertEqual(part, data)

    def test_view_only_inlines_safe_types(self):
        self.upload([("page.html", b"<script>alert(1)</script>"), ("pic.png", b"\x89PNG"), ("v.svg", b"<svg/>")])
        r, _ = self.request("GET", "/view/page.html")
        self.assertTrue(r.getheader("Content-Disposition").startswith("attachment"))
        r, _ = self.request("GET", "/view/v.svg")
        self.assertTrue(r.getheader("Content-Disposition").startswith("attachment"))
        r, _ = self.request("GET", "/view/pic.png")
        self.assertTrue(r.getheader("Content-Disposition").startswith("inline"))
        self.assertIn("sandbox", r.getheader("Content-Security-Policy"))

    def test_keep_alive_reuses_connection(self):
        c = self.conn()
        for _ in range(3):
            r, _ = self.request("GET", "/static/app.css", conn=c)
            self.assertEqual(r.status, 200)
            self.assertEqual(r.version, 11)
        c.close()

    def test_static_etag_and_gzip(self):
        r, data = self.request("GET", "/static/app.js", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(r.getheader("Content-Encoding"), "gzip")
        self.assertEqual(gzip.decompress(data), (ROOT / "static" / "app.js").read_bytes())
        etag = r.getheader("ETag")
        r, data = self.request("GET", "/static/app.js", headers={"If-None-Match": etag})
        self.assertEqual(r.status, 304)
        self.assertEqual(data, b"")
        r, data = self.request("GET", "/static/app.js")
        self.assertIsNone(r.getheader("Content-Encoding"))

    def test_error_after_unread_body_closes_connection(self):
        r, _ = self.request("POST", "/nope", body=b"x" * 10, headers={"Content-Type": "text/plain"})
        self.assertEqual(r.status, 404)
        self.assertEqual((r.getheader("Connection") or "").lower(), "close")

    def test_bad_content_length_is_400_not_500(self):
        c = self.conn()
        c.putrequest("POST", "/text")
        c.putheader("Content-Length", "abc")
        c.endheaders()
        self.assertEqual(c.getresponse().status, 400)
        c.close()

    def test_longpoll_wakes_on_new_content(self):
        r, data = self.request("GET", "/status")
        version = json.loads(data)["version"]
        got = {}

        def wait():
            t0 = time.time()
            _, d = self.request("GET", f"/status?since={version}")
            got["v"], got["dt"] = json.loads(d)["version"], time.time() - t0

        t = threading.Thread(target=wait)
        t.start()
        time.sleep(0.5)
        self.request("POST", "/text", "content=wake+up", {"Content-Type": "application/x-www-form-urlencoded"})
        t.join(8)
        self.assertGreater(got["v"], version)
        self.assertLess(got["dt"], 5)

    def test_feed_pagination_and_bundle(self):
        for i in range(3):
            self.request("POST", "/text", f"content=page-msg-{i}", {"Content-Type": "application/x-www-form-urlencoded"})
        _, data = self.request("GET", "/feed?filter=text&limit=1")
        page = data.decode()
        self.assertIn("data-load-more", page)
        self.assertEqual(page.count("<li "), 1)
        _, data = self.request("GET", "/feed?filter=text&limit=5000")
        self.assertNotIn("data-load-more", data.decode())
        _, data = self.request("GET", "/bundle?filter=all&limit=10")
        bundle = json.loads(data)
        self.assertEqual(set(bundle), {"feed", "favorites", "senders"})

    def test_server_side_search_time_and_sender_filters(self):
        form = {"Content-Type": "application/x-www-form-urlencoded", "X-Nickname": urllib.parse.quote("筛选者")}
        for i in range(5):
            self.request("POST", "/text", f"content=filler-{i}", form)
        self.request("POST", "/text", "content=needle-in-haystack", form)
        for i in range(5):
            self.request("POST", "/text", f"content=later-{i}", form)
        # limit=2 时最早那条不在第一页，但搜索仍然能命中它
        _, data = self.request("GET", "/feed?filter=text&limit=2")
        self.assertNotIn("needle-in-haystack", data.decode())
        _, data = self.request("GET", "/feed?filter=text&limit=2&q=NEEDLE")
        page = data.decode()
        self.assertIn("needle-in-haystack", page)
        self.assertEqual(page.count("<li "), 1)
        _, data = self.request("GET", "/feed?filter=text&q=" + urllib.parse.quote("不存在的关键词"))
        self.assertIn("没有符合条件", data.decode())
        _, data = self.request("GET", "/feed?filter=text&since=" + str(time.time() + 3600))
        self.assertEqual(data.decode().count("<li "), 0)
        _, data = self.request("GET", "/feed?filter=text&limit=500&sender=" + urllib.parse.quote("筛选者"))
        self.assertIn("needle-in-haystack", data.decode())
        _, data = self.request("GET", "/feed?filter=text&sender=nobody")
        self.assertEqual(data.decode().count("<li "), 0)

    def test_touch_does_not_rewrite_files_json(self):
        self.upload([("touch.txt", b"t")])
        path = Path(self.tmp.name) / "files.json"
        before = path.stat().st_mtime_ns
        for _ in range(5):
            self.request("GET", "/download/touch.txt")
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_reshare_flow(self):
        self.upload([("gone.txt", b"old")])
        entry = next(f for f in self.files() if f["name"] == "gone.txt")
        # 未过期的文件不允许重新分享
        r, _ = self.upload([("new.txt", b"new")], path="/file/reshare/" + entry["id"])
        self.assertEqual(r.status, 404)
        leftovers = [n for n in os.listdir(Path(self.tmp.name) / "shared") if n.endswith(".part")]
        self.assertEqual(leftovers, [])

    def test_cookie_is_httponly_and_valid(self):
        r, _ = self.request("GET", "/status")
        cookie = r.getheader("Set-Cookie")
        self.assertIn("cid=", cookie)
        self.assertIn("HttpOnly", cookie)
        # 伪造的 cid 会被换掉
        r, _ = self.request("GET", "/status", headers={"Cookie": "cid=<script>"})
        self.assertIn("cid=", r.getheader("Set-Cookie") or "")

    def test_forwarded_requests_are_not_host(self):
        r, data = self.request("POST", "/texts/clear", headers={"X-Forwarded-For": "10.0.0.9"})
        self.assertEqual(r.status, 403)


if __name__ == "__main__":
    unittest.main()
