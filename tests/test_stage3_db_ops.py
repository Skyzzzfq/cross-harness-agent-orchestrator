from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.db_ops import backup_database, restore_database, verify_database
from orchestrator.storage.sqlite_store import SQLiteStateStore


class DatabaseBackupRestoreTests(unittest.TestCase):
    """T6：备份 → 修改 → 恢复 → 数据回到备份点；校验通过。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db_path = root / "state.db"
        self.backup_dir = root / "backups"
        self.store = SQLiteStateStore(self.db_path)
        self.store.create_run("run-1", "team-1")
        self.store.create_task("run-1", "task-1", cwd=str(root))

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass
        self.temp.cleanup()

    def test_backup_restore_round_trip_returns_data_to_backup_point(self) -> None:
        backup = backup_database(self.db_path, self.backup_dir)
        self.assertTrue(backup.is_file())
        self.assertEqual(verify_database(self.db_path)["integrity_check"], "ok")

        # 备份后新增数据（模拟升级/修改）
        self.store.create_task("run-1", "task-2", cwd=str(self.temp.name))
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE run_id='run-1'"
        ).fetchone()[0], 2)

        # 恢复：关闭连接后 restore，重新打开
        self.store.close()
        restore_database(self.db_path, backup)
        check = verify_database(self.db_path)
        self.assertEqual(check["integrity_check"], "ok")
        self.assertEqual(check["foreign_key_errors"], 0)
        self.assertEqual(check["schema_version"], 12)

        restored = SQLiteStateStore(self.db_path)
        try:
            count = restored.connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE run_id='run-1'"
            ).fetchone()[0]
            self.assertEqual(count, 1, "restore 必须回到备份点（task-2 不应存在）")
            state = restored.connection.execute(
                "SELECT state FROM tasks WHERE task_id='task-1'"
            ).fetchone()["state"]
            self.assertEqual(state, "PENDING")
        finally:
            restored.close()

    def test_verify_reports_version_and_integrity(self) -> None:
        result = verify_database(self.db_path)
        self.assertEqual(result["integrity_check"], "ok")
        self.assertEqual(result["foreign_key_errors"], 0)
        self.assertEqual(result["schema_version"], 12)

    def test_restore_missing_backup_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            restore_database(self.db_path, Path(self.temp.name) / "nope.db")


class SchemaMigrationTests(unittest.TestCase):
    """T6：升级（v2→v12 迁移）后完整性校验通过。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_fresh_store_migrates_to_current_schema(self) -> None:
        db = Path(self.temp.name) / "migrated.db"
        store = SQLiteStateStore(db)
        try:
            version = store.connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, 12)
        finally:
            store.close()
        check = verify_database(db)
        self.assertEqual(check["integrity_check"], "ok")
        self.assertEqual(check["foreign_key_errors"], 0)


if __name__ == "__main__":
    unittest.main()
