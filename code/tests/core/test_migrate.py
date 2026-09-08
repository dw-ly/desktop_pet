"""迁移框架测试（data-consistency impl §3.2）。"""

from __future__ import annotations

import shutil

import pytest

from core.db import Database
from core.migrate import MIGRATIONS_DIR, migrate

TABLES = (
    "schema_version",
    "pairing",
    "events",
    "carries",
    "gift_offers",
    "user_items",
    "pet_state",
    "anniversaries",
)


def _make_migrations_dir(tmp_path, extra: dict[str, str]):
    d = tmp_path / "migrations"
    d.mkdir()
    shutil.copy(MIGRATIONS_DIR / "0001_init.sql", d / "0001_init.sql")
    for name, content in extra.items():
        (d / name).write_text(content, encoding="utf-8")
    return d


def _table_exists(db, name: str) -> bool:
    return bool(
        db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


def test_empty_db_initialized(tmp_path):
    """空库首次 migrate → 应用真实 0001+0002 → version=2、8 张表齐全。"""
    db = Database(tmp_path)
    try:
        migrate(db)
        assert db.get_schema_version() == 2
        for table in TABLES:
            assert _table_exists(db, table), f"缺少表: {table}"
    finally:
        db.close()


def test_incremental_script(tmp_path):
    """增量脚本：0002 应用后 version=2、新表存在。"""
    db = Database(tmp_path)
    try:
        mig = _make_migrations_dir(
            tmp_path, {"0002_add_test.sql": "CREATE TABLE test_tbl (id INTEGER);"}
        )
        migrate(db, mig)
        assert db.get_schema_version() == 2
        assert _table_exists(db, "test_tbl")
    finally:
        db.close()


def test_failing_script_rolls_back(tmp_path):
    """脚本中途失败 → 整体回滚：version 不变、已建表消失。"""
    db = Database(tmp_path)
    try:
        mig = _make_migrations_dir(
            tmp_path,
            {
                "0002_bad.sql": (
                    "CREATE TABLE test_tbl (id INTEGER);"
                    "INSERT INTO nonexistent VALUES (1);"
                )
            },
        )
        with pytest.raises(Exception):
            migrate(db, mig)
        assert db.get_schema_version() == 1
        assert not _table_exists(db, "test_tbl")
    finally:
        db.close()


def test_idempotent(tmp_path):
    """重复 migrate 无副作用、version 不变。"""
    db = Database(tmp_path)
    try:
        mig = _make_migrations_dir(
            tmp_path, {"0002_add_test.sql": "CREATE TABLE test_tbl (id INTEGER);"}
        )
        migrate(db, mig)
        migrate(db, mig)
        assert db.get_schema_version() == 2
        assert db.execute("SELECT COUNT(*) FROM test_tbl").fetchone()[0] == 0
    finally:
        db.close()


def test_ordered_by_version(tmp_path):
    """文件乱序也严格按编号升序应用。"""
    db = Database(tmp_path)
    try:
        mig = _make_migrations_dir(
            tmp_path,
            {
                "0003_add_third.sql": "CREATE TABLE tbl_third (id INTEGER);",
                "0002_add_second.sql": "CREATE TABLE tbl_second (id INTEGER);",
            },
        )
        migrate(db, mig)
        assert db.get_schema_version() == 3
        assert _table_exists(db, "tbl_second")
        assert _table_exists(db, "tbl_third")
    finally:
        db.close()


def test_non_migration_files_ignored(tmp_path):
    """非 NNNN_*.sql 文件不参与、不报错。"""
    db = Database(tmp_path)
    try:
        mig = _make_migrations_dir(tmp_path, {})
        (mig / "README.txt").write_text("hello", encoding="utf-8")
        (mig / "notes.sql").write_text("-- just notes", encoding="utf-8")
        migrate(db, mig)
        assert db.get_schema_version() == 1
        for table in TABLES:
            assert _table_exists(db, table)
    finally:
        db.close()
