"""sync_store 的 sqlite 存储层测试：读写、冲突、持久化，以及从旧版 sync.json 自动迁移。"""

import importlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path


class SyncStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["SFD_DATA_DIR"] = self.tmp.name
        self.addCleanup(os.environ.pop, "SFD_DATA_DIR", None)
        import sync_store
        self.store = importlib.reload(sync_store)  # 路径在导入时按环境变量确定，每个用例重新加载
        self.addCleanup(self._close)

    def _close(self):
        if self.store._conn is not None:
            self.store._conn.close()
            self.store._conn = None

    def put(self, folder, path, data, **kw):
        return self.store.write_file(folder, path, io.BytesIO(data), expected_length=len(data), **kw)

    def test_write_conflict_delete_and_rev_sequence(self):
        fid, err = self.store.create_folder("docs")
        self.assertEqual((fid, err), ("folder_1", ""))
        ok, err, rev1 = self.put(fid, "a.txt", b"one", base_rev=0, device="A")
        self.assertEqual((ok, rev1), (True, 1))
        # 旧的 base_rev 被拒绝，并返回当前 rev
        ok, err, cur = self.put(fid, "a.txt", b"two", base_rev=0, device="B")
        self.assertFalse(ok)
        self.assertIn("Conflict", err)
        self.assertEqual(cur, 1)
        ok, _, rev2 = self.put(fid, "a.txt", b"two", base_rev=1, device="B")
        self.assertEqual(rev2, 2)
        manifest, _ = self.store.get_manifest(fid)
        self.assertEqual(manifest["a.txt"]["size"], 3)
        self.assertEqual(manifest["a.txt"]["device"], "B")
        self.assertEqual(len(manifest["a.txt"]["sha256"]), 64)
        self.assertFalse(manifest["a.txt"]["deleted"])
        # 删除记成墓碑，rev 继续递增，旧 base_rev 删不掉
        ok, err = self.store.delete_file(fid, "a.txt", base_rev=1)
        self.assertFalse(ok)
        ok, err = self.store.delete_file(fid, "a.txt", base_rev=2, device="A")
        self.assertTrue(ok)
        manifest, _ = self.store.get_manifest(fid)
        self.assertEqual(manifest["a.txt"]["deleted"], True)
        self.assertEqual(manifest["a.txt"]["rev"], 3)
        self.assertNotIn("sha256", manifest["a.txt"])
        self.assertFalse((Path(self.tmp.name) / "synced" / fid / "a.txt").exists())
        info = self.store.get_folders()[fid]
        self.assertEqual((info["name"], info["file_count"]), ("docs", 0))

    def test_incomplete_upload_and_bad_paths(self):
        fid, _ = self.store.create_folder("x")
        ok, err, _ = self.store.write_file(fid, "short.bin", io.BytesIO(b"abc"), expected_length=10)
        self.assertFalse(ok)
        self.assertEqual(self.store.get_manifest(fid)[0], {})
        ok, err, _ = self.put(fid, "../escape.txt", b"x")
        self.assertEqual(err, "Invalid path")
        ok, err, _ = self.put("nope", "a.txt", b"x")
        self.assertEqual(err, "Folder not found")
        leftovers = [n for n in os.listdir(Path(self.tmp.name) / "synced" / fid) if n.startswith(".sfd_upload_")]
        self.assertEqual(leftovers, [])

    def test_persists_across_reconnect(self):
        fid, _ = self.store.create_folder("keep")
        self.put(fid, "sub/b.txt", b"bbb", device="A")
        self._close()
        manifest, err = self.store.get_manifest(fid)
        self.assertEqual(err, "")
        self.assertEqual(manifest["sub/b.txt"]["size"], 3)
        self.assertEqual(self.store.create_folder("second")[0], "folder_2")  # 文件夹编号不回退

    def test_delete_folder_moves_to_trash(self):
        fid, _ = self.store.create_folder("gone")
        self.put(fid, "a.txt", b"a")
        self.assertEqual(self.store.delete_folder(fid), "")
        self.assertNotIn(fid, self.store.get_folders())
        self.assertEqual(self.store.get_manifest(fid)[1], "Folder not found")
        self.assertEqual(self.store.delete_folder(fid), "Folder not found")
        trash = os.listdir(Path(self.tmp.name) / "synced" / ".trash")
        self.assertEqual(len(trash), 1)

    def test_migrates_legacy_sync_json(self):
        legacy = {
            "next_folder_id": 3,
            "folders": {"folder_2": {
                "name": "old", "created": 1700000000, "next_rev": 5,
                "manifest": {
                    "keep.txt": {"size": 4, "mtime": 1700000100, "sha256": "ab" * 32, "rev": 3, "deleted": False, "device": "X"},
                    "gone.txt": {"deleted": True, "rev": 4, "device": "Y", "mtime": 1700000200},
                },
            }},
        }
        Path(self.tmp.name, "sync.json").write_text(json.dumps(legacy))
        manifest, err = self.store.get_manifest("folder_2")
        self.assertEqual(err, "")
        self.assertEqual(manifest["keep.txt"], legacy["folders"]["folder_2"]["manifest"]["keep.txt"])
        self.assertEqual(manifest["gone.txt"], legacy["folders"]["folder_2"]["manifest"]["gone.txt"])
        self.assertEqual(self.store.get_folders()["folder_2"]["file_count"], 1)
        # 旧文件改名留作备份，next_rev / next_folder_id 沿用
        self.assertFalse(Path(self.tmp.name, "sync.json").exists())
        self.assertTrue(Path(self.tmp.name, "sync.json.migrated").exists())
        self.assertEqual(self.store.create_folder("new")[0], "folder_3")
        self.put("folder_2", "keep.txt", b"new!", base_rev=3)
        self.assertEqual(self.store.get_manifest("folder_2")[0]["keep.txt"]["rev"], 5)

    def test_corrupt_legacy_json_is_left_alone(self):
        Path(self.tmp.name, "sync.json").write_text("{not json")
        self.assertEqual(self.store.get_folders(), {})
        self.assertTrue(Path(self.tmp.name, "sync.json").exists())


if __name__ == "__main__":
    unittest.main()
