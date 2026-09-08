"""统一数据库连接管理（对应 data-consistency impl §2 / plan G1）。

- 单连接 + threading.RLock 串行化所有 DB 操作；连接 check_same_thread=False
- isolation_level=None（autocommit）：单条 execute 立即生效
- PRAGMA：journal_mode=WAL / synchronous=NORMAL / foreign_keys=ON / busy_timeout=5000
- row_factory=sqlite3.Row：便捷查询 query_all/query_one 返回行对象（支持 row["col"] / row[0]）
- 多步写操作显式经 transaction()；支持 savepoint 嵌套（内层回滚不影响外层）
- 迁移脚本经 run_script_in_transaction（单事务原子，禁止嵌套在 transaction() 内）
- 启动入口 init_core(data_dir)：建目录 → 连接 → migrate 到最新版本
- core.db 位于 SyncConfig.data_dir
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_NAME = "core.db"

_SCHEMA_VERSION_SQL = (
    "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
)


def _strip_comments(sql_text: str) -> str:
    """剥离行首/行尾 `--` 注释（迁移脚本为受控 DDL，不含字符串字面量中的 `--`）。"""
    lines = []
    for line in sql_text.splitlines():
        idx = line.find("--")
        if idx != -1:
            line = line[:idx]
        lines.append(line)
    return "\n".join(lines)


class Database:
    """统一数据库门面。所有访问经本类；内部单连接 + 锁。"""

    def __init__(self, data_dir: str | Path) -> None:
        p = Path(data_dir)
        p.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(p / DB_NAME), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._tx_depth = 0
        self._closed = False

    # ------------------------------------------------------------------ #
    # 基本执行（autocommit：单条语句立即生效）
    # ------------------------------------------------------------------ #

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, seq_of_params: list[tuple]) -> None:
        with self._lock:
            self._conn.executemany(sql, seq_of_params)

    # ------------------------------------------------------------------ #
    # 便捷查询（row_factory=sqlite3.Row）
    # ------------------------------------------------------------------ #

    def query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """查询多行；返回 list[sqlite3.Row]（row["col"] 或 row[0] 访问）。"""
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        """查询单行；无结果返回 None。"""
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def table_exists(self, name: str) -> bool:
        """schema 中是否存在指定表。"""
        with self._lock:
            return (
                self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (name,),
                ).fetchone()
                is not None
            )

    # ------------------------------------------------------------------ #
    # 事务（支持 savepoint 嵌套）
    # ------------------------------------------------------------------ #

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """事务：最外层 BEGIN/COMMIT，内层 SAVEPOINT/RELEASE。

        - 内层异常回滚到 savepoint（不影响外层已写内容）并继续上抛；
          外层可捕获后继续，或在最外层整体回滚。
        - 全程持锁，串行化所有 DB 操作。
        """
        with self._lock:
            if self._tx_depth == 0:
                self._conn.execute("BEGIN")
            else:
                sp = f"sp_{self._tx_depth}"
                self._conn.execute(f"SAVEPOINT {sp}")
            self._tx_depth += 1
            try:
                yield
            except BaseException:
                self._tx_depth -= 1
                if self._tx_depth == 0:
                    self._conn.execute("ROLLBACK")
                else:
                    self._conn.execute(f"ROLLBACK TO {sp}")
                    self._conn.execute(f"RELEASE {sp}")
                raise
            else:
                self._tx_depth -= 1
                if self._tx_depth == 0:
                    self._conn.execute("COMMIT")
                else:
                    self._conn.execute(f"RELEASE {sp}")

    def run_script_in_transaction(
        self, sql_text: str, version: int | None = None
    ) -> None:
        """单事务执行多语句脚本（以 ';' 分隔），可选写入 schema 版本号。任一失败整体回滚。

        迁移专用：禁止嵌套在 transaction() 内。
        """
        if self._tx_depth:
            raise RuntimeError(
                "run_script_in_transaction 不能嵌套在 transaction() 内"
            )
        cleaned = _strip_comments(sql_text)
        statements = [s.strip() for s in cleaned.split(";") if s.strip()]
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                for stmt in statements:
                    self._conn.execute(stmt)
                if version is not None:
                    self._conn.execute(
                        "INSERT INTO schema_version(version) VALUES (?)", (version,)
                    )
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    # ------------------------------------------------------------------ #
    # schema 版本
    # ------------------------------------------------------------------ #

    def get_schema_version(self) -> int:
        """无 schema_version 表（空库/旧库）→ 0。"""
        with self._lock:
            if not self.table_exists("schema_version"):
                return 0
            row = self._conn.execute(_SCHEMA_VERSION_SQL).fetchone()
            return int(row[0]) if row else 0

    def set_schema_version(self, version: int) -> None:
        """写入版本号（迁移框架在事务内调用；单次调用为 autocommit）。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO schema_version(version) VALUES (?)", (version,)
            )

    def close(self) -> None:
        """关闭连接；幂等（重复调用无副作用）。"""
        with self._lock:
            if self._closed:
                return
            self._conn.close()
            self._closed = True


def init_core(data_dir: str | Path) -> Database:
    """启动入口：建目录 → 打开 core.db → 迁移到最新 schema 版本。

    业务代码在应用启动序列中调用一次；迁移失败抛异常，由上层中止启动。
    """
    from .migrate import migrate  # 延迟导入避免循环

    db = Database(data_dir)
    try:
        migrate(db)
    except BaseException:
        db.close()
        raise
    return db
