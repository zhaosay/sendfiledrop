#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sendfiledrop 同步代理端到端测试

在临时目录启动服务器，用两个代理进行同步测试
"""

import os
import sys
import json
import time
import shutil
import tempfile
import subprocess
import urllib.request
import urllib.parse
import threading

def wait_for_server(url, timeout=10):
    """等待服务器启动"""
    start = time.time()
    while time.time() - start < timeout:
        try:
            urllib.request.urlopen(f"{url}/api/sync/folders", timeout=2)
            return True
        except:
            time.sleep(0.2)
    return False

def run_agent_once(server_url, folder_name, local_dir, device_name):
    """运行一次代理同步"""
    cmd = [
        sys.executable, "sync_client.py",
        server_url,
        "--folder", folder_name,
        "--dir", local_dir,
        "--device", device_name,
        "--once"
    ]
    try:
        result = subprocess.run(cmd, cwd=os.path.dirname(__file__),
                              capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except:
        return False

def read_file(path):
    """读取文件内容"""
    try:
        with open(path, 'r') as f:
            return f.read()
    except:
        return None

def write_file(path, content):
    """写入文件"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(content)

def test_sync():
    """运行同步测试"""
    test_dir = tempfile.mkdtemp(prefix="sendfiledrop_test_")
    shared_dir = os.path.join(test_dir, "shared")
    dir_a = os.path.join(test_dir, "dir_a")
    dir_b = os.path.join(test_dir, "dir_b")

    os.makedirs(shared_dir)
    os.makedirs(dir_a)
    os.makedirs(dir_b)

    print(f"测试目录: {test_dir}")

    # 启动服务器
    server_process = subprocess.Popen(
        [sys.executable, "server.py", "8130"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        env={**os.environ, "SFD_DATA_DIR": shared_dir},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

    time.sleep(2)

    if not wait_for_server("http://127.0.0.1:8130"):
        print("服务器启动失败")
        server_process.terminate()
        return False

    print("服务器已启动 (端口 8130)")

    try:
        # 创建同步文件夹
        url = "http://127.0.0.1:8130/api/sync/folders"
        req = urllib.request.Request(url, data=b"name=TestSync", method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req) as r:
            resp = json.loads(r.read())
            folder_id = resp.get("folder_id")
            print(f"创建文件夹: {folder_id}")

        # 测试 1: A 新增文件 -> B 出现
        print("\n测试 1: A 新增文件...")
        write_file(os.path.join(dir_a, "test.txt"), "Hello from A")
        run_agent_once("http://127.0.0.1:8130", "TestSync", dir_a, "device_a")
        run_agent_once("http://127.0.0.1:8130", "TestSync", dir_b, "device_b")

        content_b = read_file(os.path.join(dir_b, "test.txt"))
        if content_b == "Hello from A":
            print("成功: B 接收到 A 的文件")
        else:
            print(f"失败: B 的文件内容为 {content_b}")
            return False

        # 测试 2: B 修改 -> A 更新
        print("\n测试 2: B 修改文件...")
        write_file(os.path.join(dir_b, "test.txt"), "Modified by B")
        run_agent_once("http://127.0.0.1:8130", "TestSync", dir_b, "device_b")
        run_agent_once("http://127.0.0.1:8130", "TestSync", dir_a, "device_a")

        content_a = read_file(os.path.join(dir_a, "test.txt"))
        if content_a == "Modified by B":
            print("成功: A 收到 B 的修改")
        else:
            print(f"失败: A 的文件内容为 {content_a}")
            return False

        # 测试 3: A 删除 -> B 删除
        print("\n测试 3: A 删除文件...")
        os.unlink(os.path.join(dir_a, "test.txt"))
        run_agent_once("http://127.0.0.1:8130", "TestSync", dir_a, "device_a")
        run_agent_once("http://127.0.0.1:8130", "TestSync", dir_b, "device_b")

        if not os.path.exists(os.path.join(dir_b, "test.txt")):
            print("成功: B 的文件已删除")
        else:
            print("失败: B 的文件仍存在")
            return False

        # 测试 4: 冲突处理
        print("\n测试 4: 冲突处理...")
        write_file(os.path.join(dir_a, "conflict.txt"), "A version")
        write_file(os.path.join(dir_b, "conflict.txt"), "B version")

        run_agent_once("http://127.0.0.1:8130", "TestSync", dir_a, "device_a")
        run_agent_once("http://127.0.0.1:8130", "TestSync", dir_b, "device_b")
        run_agent_once("http://127.0.0.1:8130", "TestSync", dir_a, "device_a")

        # 检查冲突文件
        conflict_found = False
        for f in os.listdir(dir_a):
            if "conflict" in f and "冲突" in f:
                conflict_found = True
                print(f"成功: A 中发现冲突文件 {f}")
                break

        if not conflict_found:
            print("警告: 未找到冲突文件")

        # 测试 5: 空文件
        print("\n测试 5: 空文件同步...")
        write_file(os.path.join(dir_a, "empty.txt"), "")
        run_agent_once("http://127.0.0.1:8130", "TestSync", dir_a, "device_a")
        run_agent_once("http://127.0.0.1:8130", "TestSync", dir_b, "device_b")

        if os.path.exists(os.path.join(dir_b, "empty.txt")):
            print("成功: 空文件已同步")
        else:
            print("失败: 空文件未同步")
            return False

        # 测试 6: 子目录
        print("\n测试 6: 子目录文件...")
        write_file(os.path.join(dir_a, "subdir", "nested.txt"), "Nested content")
        run_agent_once("http://127.0.0.1:8130", "TestSync", dir_a, "device_a")
        run_agent_once("http://127.0.0.1:8130", "TestSync", dir_b, "device_b")

        content = read_file(os.path.join(dir_b, "subdir", "nested.txt"))
        if content == "Nested content":
            print("成功: 子目录文件已同步")
        else:
            print("失败: 子目录文件同步失败")
            return False

        print("\n测试通过！")
        return True

    finally:
        server_process.terminate()
        server_process.wait(timeout=5)
        shutil.rmtree(test_dir, ignore_errors=True)
        print(f"清理测试目录")

if __name__ == "__main__":
    success = test_sync()
    sys.exit(0 if success else 1)
