"""换机/重装恢复（对应 data-consistency impl §4.5 / plan S6）。

- `export_backup(db, passphrase, out_path)`：用户口令 → scrypt 派生密钥 →
  SecretBox 加密快照（`pet_state` + `user_items` + `anniversaries` 三表；
  **不含** 会话密钥 / 事件日志 / 配对——密钥归 sync key_store 单一职责，
  备份不含会话密钥，spec §3.4）→ `.tuanzi.bak`。
- `import_backup(db, passphrase, in_path)`：口令解密 → 事务内覆盖写三表。
  **先解密成功才写库**，错误口令明确报错且不破坏现有库。

隐私（spec §3.4/§6 决策）：备份为密文，中继不可解；口令由用户自行保管，
遗忘则无法恢复；口令不入 keyring、不存盘。口令派生独立于配对状态——换机
重新配对（新会话密钥）后旧备份仍可解密（口令不变）。

文件格式：`MAGIC + salt(SCRYPT_SALTBYTES) + SecretBox(JSON 快照)`，
salt 随机、随文件保存，解密时以同一口令 + salt 重新派生密钥。

恢复后的对齐：`import_backup` 只负责写库；调用方（恢复流程）导入后触发一次
`daily_align`（date.sync）与近期事件补拉，以备份快照 + 对方端近期事件为准
对齐，不依赖已清理的历史日志（spec §3.3.9）。
"""

from __future__ import annotations

import json
from pathlib import Path

from nacl.exceptions import CryptoError as NaClCryptoError
from nacl.pwhash import (
    SCRYPT_MEMLIMIT_INTERACTIVE,
    SCRYPT_OPSLIMIT_INTERACTIVE,
    SCRYPT_SALTBYTES,
    scrypt,
)
from nacl.secret import SecretBox
from nacl.utils import random

from .db import Database

MAGIC = b"TUANZI-BACKUP-1\n"

# 备份范围：养成状态 + 装扮解锁 + 纪念日（impl §4.5 / plan §3.2 S6）
BACKUP_TABLES = ("pet_state", "user_items", "anniversaries")


class BackupError(Exception):
    """备份相关错误（口令错误 / 文件损坏 / 非备份文件）。"""


# --------------------------------------------------------------------------- #
# 密钥派生
# --------------------------------------------------------------------------- #

def _derive_key(passphrase: str, salt: bytes) -> bytes:
    """scrypt 口令派生 32B SecretBox 密钥（opslimit/memlimit INTERACTIVE）。"""
    return scrypt.kdf(
        32,
        passphrase.encode("utf-8"),
        salt,
        SCRYPT_OPSLIMIT_INTERACTIVE,
        SCRYPT_MEMLIMIT_INTERACTIVE,
    )

def _replace_all(db: Database, table: str, rows: list[dict]) -> None:
    """清空并重写一张表（导入专用；table 为代码内受控常量）。"""
    db.execute(f"DELETE FROM {table}")
    for r in rows:
        cols = list(r)
        db.execute(
            f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
            tuple(r[c] for c in cols),
        )


# --------------------------------------------------------------------------- #
# 导出 / 导入
# --------------------------------------------------------------------------- #

def export_backup(db: Database, passphrase: str, out_path: str | Path) -> int:
    """加密导出三表快照到 `.tuanzi.bak`，返回导出行数。"""
    snap: dict[str, list[dict]] = {}
    total = 0
    for table in BACKUP_TABLES:
        rows = [dict(r) for r in db.query_all(f"SELECT * FROM {table}")]
        snap[table] = rows
        total += len(rows)
    data = json.dumps(snap, ensure_ascii=False).encode("utf-8")
    salt = random(SCRYPT_SALTBYTES)
    blob = SecretBox(_derive_key(passphrase, salt)).encrypt(data)
    Path(out_path).write_bytes(MAGIC + salt + blob)
    return total


def import_backup(db: Database, passphrase: str, in_path: str | Path) -> int:
    """解密导入三表（单事务覆盖写），返回导入行数。

    错误口令 / 损坏文件 / 非备份文件 → 抛 `BackupError`，**不写库**。
    """
    raw = Path(in_path).read_bytes()
    if not raw.startswith(MAGIC):
        raise BackupError("不是有效的团子备份文件")
    salt_off = len(MAGIC)
    if len(raw) < salt_off + SCRYPT_SALTBYTES:
        raise BackupError("备份文件不完整")
    salt = raw[salt_off : salt_off + SCRYPT_SALTBYTES]
    blob = raw[salt_off + SCRYPT_SALTBYTES :]
    key = _derive_key(passphrase, salt)
    try:
        plain = SecretBox(key).decrypt(blob)
    except NaClCryptoError as exc:
        raise BackupError("口令错误或备份已损坏，未改动现有数据") from exc
    try:
        snap = json.loads(plain.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("备份数据损坏") from exc
    if not isinstance(snap, dict) or not all(t in snap for t in BACKUP_TABLES):
        raise BackupError("备份数据缺少必需表")

    with db.transaction():
        for table in BACKUP_TABLES:
            _replace_all(db, table, snap[table])
    return sum(len(snap[t]) for t in BACKUP_TABLES)
