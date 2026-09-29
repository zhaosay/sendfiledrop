#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sendfiledrop 同步代理客户端
在本地电脑上运行，监听文件夹变化并与主机同步

用法:
    python3 sync_client.py http://主机地址:端口 --folder 文件夹名 --dir /本地目录 [--once]
"""

import os
import sys
import json
import time
import hashlib
import urllib.request
import urllib.error
import urllib.parse
import argparse
import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple

AGENT_VERSION = "0.3.0"
PROTOCOL = 1

def sha256_file(path: str) -> str:
    """计算文件哈希"""
    h = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except (OSError, IOError):
        return ""

def scan_local(root: str) -> Dict[str, Dict]:
    """扫描本地目录，返回 {相对路径: {size, mtime, sha256}}"""
    result = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith('.')]
        for filename in filenames:
            if filename.startswith('.') or filename == '.sfd_state.json':
                continue
            fullpath = os.path.join(dirpath, filename)
            if os.path.islink(fullpath):
                continue
            try:
                stat = os.stat(fullpath)
                relpath = os.path.relpath(fullpath, root)
                result[relpath] = {
                    'size': stat.st_size,
                    'mtime': int(stat.st_mtime),
                    'sha256': sha256_file(fullpath)
                }
            except (OSError, IOError):
                pass
    return result

def load_state(root: str) -> Dict[str, Dict]:
    """加载本地状态文件"""
    state_file = os.path.join(root, '.sfd_state.json')
    if not os.path.exists(state_file):
        return {}
    try:
        with open(state_file, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

def save_state(root: str, state: Dict):
    """保存本地状态文件"""
    state_file = os.path.join(root, '.sfd_state.json')
    try:
        temp_fd, temp_path = tempfile.mkstemp(dir=root)
        with os.fdopen(temp_fd, 'w') as f:
            json.dump(state, f)
        os.replace(temp_path, state_file)
    except (IOError, OSError):
        pass

def decide(local: Dict, remote: Dict, base: Dict, device_name: str) -> list:
    """
    三方比较，返回操作列表
    操作格式: ('upload' | 'download' | 'delete' | 'mkdir', path, 信息)
    """
    actions = []
    local = local or {}
    remote = remote or {}
    base = base or {}
    all_paths = set(local.keys()) | set(remote.keys()) | set(base.keys())

    for path in sorted(all_paths):
        l = local.get(path, {})
        r = remote.get(path, {})
        b = base.get(path, {})

        l_hash = l.get('sha256', '')
        r_hash = r.get('sha256', '')
        b_hash = b.get('sha256', '')

        l_exists = bool(l)
        r_exists = bool(r) and not r.get('deleted')
        b_exists = bool(b) and not b.get('deleted')

        if l_exists and r_exists and l_hash == r_hash:
            continue
        elif l_exists and not b_exists and not r_exists:
            actions.append(('upload', path, l))
        elif not l_exists and not b_exists and r_exists:
            actions.append(('download', path, r))
        elif l_exists and b_exists and not r_exists:
            actions.append(('download', path, r))
        elif not l_exists and b_exists and r_exists:
            actions.append(('download', path, r))
        elif l_exists and r_exists and l_hash != r_hash:
            if l_hash != b_hash and r_hash != b_hash:
                timestamp = int(time.time())
                parts = path.rsplit('.', 1)
                if len(parts) == 2:
                    conflict_path = f"{parts[0]} (冲突 {device_name} {timestamp}).{parts[1]}"
                else:
                    conflict_path = f"{path} (冲突 {device_name} {timestamp})"
                actions.append(('rename_upload', path, conflict_path, l))
                actions.append(('download', path, r))
            elif l_hash != b_hash:
                actions.append(('upload', path, l))
            else:
                actions.append(('download', path, r))
        elif l_exists and not b_exists and r_exists and l_hash != r_hash:
            actions.append(('upload', path, l))
        elif not l_exists and b_exists and r_exists:
            actions.append(('download', path, r))

    for path in sorted(base.keys()):
        if path not in local and path not in remote:
            continue
        if path not in local and path in remote and not remote[path].get('deleted'):
            continue
        if path not in local and path in base:
            actions.append(('delete_local', path))

    return actions

def http_request(url: str, method: str = 'GET', data: bytes = None, headers: dict = None) -> Tuple[bool, dict]:
    """执行 HTTP 请求，返回 (成功, 数据/错误)"""
    try:
        req = urllib.request.Request(url, data=data, method=method)
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status >= 400:
                return False, {'error': f"HTTP {r.status}"}
            try:
                return True, json.loads(r.read().decode('utf-8'))
            except json.JSONDecodeError:
                return True, {}
    except urllib.error.HTTPError as e:
        try:
            return False, json.loads(e.read().decode('utf-8'))
        except:
            return False, {'error': str(e)}
    except (urllib.error.URLError, Exception) as e:
        return False, {'error': str(e)}

def sync_once(server_url: str, folder_name: str, folder_id: str, local_dir: str, device_name: str) -> bool:
    """执行一次同步循环"""
    local = scan_local(local_dir)
    state = load_state(local_dir)

    ok, resp = http_request(f"{server_url}/api/sync/manifest?folder={folder_id}")
    if not ok:
        print(f"获取 manifest 失败: {resp}")
        return False

    remote = {k: v for k, v in resp.get('manifest', {}).items()}
    base = state or {}

    actions = decide(local, remote, base, device_name)

    for action in actions:
        if action[0] == 'upload':
            path, info = action[1], action[2]
            fullpath = os.path.join(local_dir, path)
            try:
                with open(fullpath, 'rb') as f:
                    url = f"{server_url}/api/sync/file?folder={folder_id}&path={urllib.parse.quote(path)}&base_rev={state.get(path, {}).get('rev', 0)}&device={device_name}"
                    ok, resp = http_request(url, 'PUT', data=f.read(), headers={'Content-Type': 'application/octet-stream'})
                    if ok:
                        new_rev = resp.get('rev', 0)
                        state[path] = {'rev': new_rev, 'sha256': info['sha256'], 'mtime': info['mtime'], 'size': info['size']}
                        print(f"上传: {path}")
                    else:
                        print(f"上传失败: {path} - {resp}")
            except Exception as e:
                print(f"上传异常: {path} - {e}")

        elif action[0] == 'download':
            path, info = action[1], action[2]
            fullpath = os.path.join(local_dir, path)
            try:
                dirpath = os.path.dirname(fullpath)
                if dirpath:
                    os.makedirs(dirpath, exist_ok=True)
                else:
                    dirpath = local_dir

                url = f"{server_url}/api/sync/file?folder={folder_id}&path={urllib.parse.quote(path)}"
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=10) as r:
                    temp_fd, temp_path = tempfile.mkstemp(dir=dirpath)
                    try:
                        bytes_written = 0
                        while True:
                            chunk = r.read(65536)
                            if not chunk:
                                break
                            os.write(temp_fd, chunk)
                            bytes_written += len(chunk)
                        os.close(temp_fd)
                        os.replace(temp_path, fullpath)
                        if 'mtime' in info:
                            os.utime(fullpath, (info['mtime'], info['mtime']))
                        state[path] = {'rev': info.get('rev', 0), 'sha256': info.get('sha256', ''), 'mtime': info.get('mtime', 0), 'size': info.get('size', 0)}
                        print(f"下载: {path}")
                    except Exception as e:
                        try:
                            os.close(temp_fd)
                        except:
                            pass
                        try:
                            os.unlink(temp_path)
                        except:
                            pass
                        raise
            except Exception as e:
                print(f"下载异常: {path} - {e}")

        elif action[0] == 'delete_local':
            path = action[1]
            fullpath = os.path.join(local_dir, path)
            try:
                if os.path.exists(fullpath):
                    os.unlink(fullpath)
                state.pop(path, None)
                print(f"删除: {path}")
            except Exception as e:
                print(f"删除异常: {path} - {e}")

    save_state(local_dir, state)
    return True

def main():
    parser = argparse.ArgumentParser(description='sendfiledrop 同步代理')
    parser.add_argument('server_url', help='服务器地址，如 http://192.168.1.10:8000')
    parser.add_argument('--folder', required=True, help='文件夹名称')
    parser.add_argument('--dir', required=True, help='本地目录路径')
    parser.add_argument('--once', action='store_true', help='运行一次后退出')
    parser.add_argument('--device', default=None, help='设备名称（默认自动生成）')

    args = parser.parse_args()

    if not os.path.isdir(args.dir):
        print(f"错误：目录不存在 {args.dir}")
        sys.exit(1)

    device_name = args.device or f"agent-{os.urandom(4).hex()}"

    ok, resp = http_request(f"{args.server_url}/api/sync/folders")
    if not ok:
        print(f"连接失败: {resp}")
        sys.exit(1)

    folders = resp.get('folders', {})
    folder_id = None
    for fid, info in folders.items():
        if info.get('name') == args.folder:
            folder_id = fid
            break

    if not folder_id:
        print(f"找不到文件夹: {args.folder}")
        sys.exit(1)

    print(f"连接到: {args.server_url}/{args.folder}")
    print(f"设备: {device_name}")
    print(f"目录: {args.dir}")

    if args.once:
        sync_once(args.server_url, args.folder, folder_id, args.dir, device_name)
    else:
        while True:
            try:
                sync_once(args.server_url, args.folder, folder_id, args.dir, device_name)
            except KeyboardInterrupt:
                print("已停止")
                sys.exit(0)
            except Exception as e:
                print(f"同步异常: {e}")
            time.sleep(5)

if __name__ == '__main__':
    main()
