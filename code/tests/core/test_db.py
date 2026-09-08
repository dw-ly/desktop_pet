"""统一数据库连接管理测试（data-consistency impl §2.4）。"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from core.db import Database, init_core

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


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path)
    yield d
    d.close()


def test_db_file_created(tmp_path):
    Database(tmp_path).close()
    assert (tmp_path / "core.db").exists()


def test_wal_enabled(db):
    row = db.execute("PRAGMA journal_mode").fetchone()
    assert row[0].lower() == "wal"


def test_foreign_keys_on(db):
    row = db.execute("PRAGMA foreign_keys").fetchone()
    assert row[0] == 1


def test_empty_schema_version_is_zero(db):
    assert db.get_schema_version() == 0


def test_execute_autocommit(db, tmp_path):
    """单条 execute 立即生效：同库可查，且新连接（同文件）可见。"""
    db.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v INTEGER)")
    db.execute("INSERT INTO t VALUES ('a', 1)")
    assert db.execute("SELECT v FROM t WHERE k='a'").fetchone()[0] == 1
    d2 = Database(tmp_path)
    try:
        assert d2.execute("SELECT v FROM t WHERE k='a'").fetchone()[0] == 1
    finally:
        d2.close()


def test_transaction_commit(db):
    db.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v INTEGER)")
    with db.transaction():
        db.execute("INSERT INTO t VALUES ('a', 1)")
        db.execute("INSERT INTO t VALUES ('b', 2)")
    assert db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2


def test_transaction_rollback(db):
    db.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v INTEGER)")
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.execute("INSERT INTO t VALUES ('a', 1)")
            raise RuntimeError("boom")
    assert db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_run_script_atomic_success(db):
    db.run_script_in_transaction(
        "CREATE TABLE t (k TEXT PRIMARY KEY);"
        "CREATE TABLE u (k TEXT PRIMARY KEY);"
    )
    assert db.execute("SELECT name FROM sqlite_master WHERE name='t'").fetchone()
    assert db.execute("SELECT name FROM sqlite_master WHERE name='u'").fetchone()


def test_run_script_atomic_fail(db):
    """脚本中途失败 → 整体回滚（含已成功的表）。"""
    with pytest.raises(sqlite3.OperationalError):
        db.run_script_in_transaction(
            "CREATE TABLE t (k TEXT PRIMARY KEY);"
            "INSERT INTO nope VALUES (1);"
        )
    assert not db.execute(
        "SELECT name FROM sqlite_master WHERE name='t'"
    ).fetchone()


def test_concurrent_writes(db):
    """多线程并发写：无串扰、无 ProgrammingError。"""
    db.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v INTEGER)")
    errors: list[Exception] = []

    def worker(start: int) -> None:
        try:
            for i in range(50):
                db.execute("INSERT INTO t VALUES (?, ?)", (f"{start}-{i}", i))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(0,)),
        threading.Thread(target=worker, args=(1,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 100


# ------------------------------------------------------------------ #
# 便捷查询（row_factory=sqlite3.Row）
# ------------------------------------------------------------------ #


def test_query_all_returns_rows(db):
    db.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v INTEGER)")
    db.executemany("INSERT INTO t VALUES (?, ?)", [("a", 1), ("b", 2)])
    rows = db.query_all("SELECT * FROM t ORDER BY k")
    assert len(rows) == 2
    assert rows[0]["k"] == "a"  # 列名访问
    assert rows[0][1] == 1  # 下标访问
    assert rows[1]["v"] == 2


def test_query_one_none_and_row(db):
    db.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v INTEGER)")
    assert db.query_one("SELECT * FROM t") is None
    db.execute("INSERT INTO t VALUES ('a', 1)")
    row = db.query_one("SELECT * FROM t WHERE k='a'")
    assert row is not None
    assert row["k"] == "a"
    assert row["v"] == 1


def test_table_exists(db):
    db.execute("CREATE TABLE t (k TEXT PRIMARY KEY)")
    assert db.table_exists("t")
    assert not db.table_exists("nope")


# ------------------------------------------------------------------ #
# 事务 savepoint 嵌套
# ------------------------------------------------------------------ #


def test_transaction_nested_commit(db):
    db.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v INTEGER)")
    with db.transaction():
        db.execute("INSERT INTO t VALUES ('a', 1)")
        with db.transaction():
            db.execute("INSERT INTO t VALUES ('b', 2)")
    assert db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2


def test_transaction_nested_outer_rolls_back_all(db):
    """内层异常未被捕获 → 外层也整体回滚。"""
    db.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v INTEGER)")
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.execute("INSERT INTO t VALUES ('a', 1)")
            with db.transaction():
                db.execute("INSERT INTO t VALUES ('b', 2)")
                raise RuntimeError("inner boom")
    assert db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_transaction_nested_inner_rollback_outer_continues(db):
    """内层异常被外层捕获 → 回滚到 savepoint，外层已写内容保留，可继续提交。"""
    db.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v INTEGER)")
    with db.transaction():
        db.execute("INSERT INTO t VALUES ('a', 1)")
        try:
            with db.transaction():
                db.execute("INSERT INTO t VALUES ('b', 2)")
                raise RuntimeError("inner boom")
        except RuntimeError:
            pass
        db.execute("INSERT INTO t VALUES ('c', 3)")
    rows = db.query_all("SELECT k FROM t ORDER BY k")
    assert [r["k"] for r in rows] == ["a", "c"]


def test_run_script_rejects_nesting(db):
    """迁移脚本禁止嵌套在 transaction() 内。"""
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.run_script_in_transaction("CREATE TABLE t (k TEXT PRIMARY KEY)")


# ------------------------------------------------------------------ #
# 启动入口 init_core
# ------------------------------------------------------------------ #


def test_init_core_migrates(tmp_path):
    """init_core：建目录 + 打开 + 迁移到最新版本（version=2、8 张表）。"""
    db = init_core(tmp_path)
    try:
        assert (tmp_path / "core.db").exists()
        assert db.get_schema_version() == 2
        for table in TABLES:
            assert db.table_exists(table), f"缺少表: {table}"
    finally:
        db.close()


def test_close_idempotent(db):
    db.close()
    db.close()  # 重复关闭无副作用
