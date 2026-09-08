"""迁移执行框架（对应 data-consistency impl §3 / plan G2）。

- 启动时调用 migrate(db)：应用所有编号 > 当前版本的迁移脚本
- 每个脚本一个事务（含版本号写入），中途失败整体回滚（版本不变，应用不启动并提示）
- 幂等：已应用版本跳过；重复执行无副作用
- 约定：文件命名 `NNNN_name.sql`（4 位编号 + 下划线 + 语义名）；禁止修改已发布脚本
"""

from __future__ import annotations

import re
from pathlib import Path

from .db import Database

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIG_PATTERN = re.compile(r"^(\d{4})_.*\.sql$")


def _discover(migrations_dir: Path) -> list[tuple[int, Path]]:
    """按编号升序发现迁移脚本；非 NNNN_*.sql 文件忽略。"""
    scripts = []
    for p in sorted(migrations_dir.glob("*.sql")):
        m = _MIG_PATTERN.match(p.name)
        if m:
            scripts.append((int(m.group(1)), p))
    return scripts


def migrate(db: Database, migrations_dir: Path | None = None) -> None:
    """应用所有编号 > 当前版本的迁移脚本。幂等。"""
    base = migrations_dir or MIGRATIONS_DIR
    current = db.get_schema_version()
    for version, path in _discover(base):
        if version <= current:
            continue
        db.run_script_in_transaction(
            path.read_text(encoding="utf-8"), version=version
        )
