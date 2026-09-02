"""T6：数据库备份、恢复与校验（含 rollback 演练基础）。

使用 SQLite 在线备份 API（``Connection.backup``）保证一致性，不依赖文件拷贝，
避免连接锁冲突。升级/降级演练通过 restore 备份实现（降级 = 恢复到旧 schema
备份），恢复到任意备份点后必须通过 ``verify_database`` 校验。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path


def backup_database(
    db_path: Path, backup_dir: Path, *, name: str | None = None
) -> Path:
    """在线一致性备份，返回备份文件路径。"""
    db_path = db_path.resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    if name is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"agent-hub-{stamp}.db"
    destination = backup_dir / name
    source_conn = sqlite3.connect(db_path)
    try:
        target_conn = sqlite3.connect(destination)
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
    finally:
        source_conn.close()
    return destination


def restore_database(db_path: Path, backup_file: Path) -> None:
    """从备份文件恢复到目标数据库（目标文件会被覆盖）。"""
    db_path = db_path.resolve()
    backup_file = backup_file.resolve()
    if not backup_file.is_file():
        raise FileNotFoundError(f"backup file does not exist: {backup_file}")
    backup_conn = sqlite3.connect(backup_file)
    try:
        target_conn = sqlite3.connect(db_path)
        try:
            backup_conn.backup(target_conn)
        finally:
            target_conn.close()
    finally:
        backup_conn.close()


def verify_database(db_path: Path) -> dict[str, int | str]:
    """完整性校验：integrity_check、foreign_key_check、schema 版本。"""
    conn = sqlite3.connect(db_path)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        return {
            "integrity_check": integrity,
            "foreign_key_errors": len(foreign_key_errors),
            "schema_version": version,
        }
    finally:
        conn.close()
