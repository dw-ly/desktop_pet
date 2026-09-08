"""设置/备份服务层（对应 data-consistency impl §4.6 / plan C1）。

UI 无关编排，供 Qt 对话框（`settings_dialog.py`）与工具脚本复用：
- `passphrase_strength`：口令强度提示（UI 展示用）。
- `export_backup_flow`：口令校验 → 加密导出三表快照到 `.tuanzi.bak`。
- `import_backup_flow`：解密导入 → 恢复后对齐（date.sync 快照；**不做重放
  修复**）——恢复后亲密度以备份快照为准（spec §3.3.9），换机重新配对后新
  会话密钥无法验证旧事件签名，旧事件流不可重放，故对齐传 `mac_key=None`
  仅发快照（`daily_align` 跳过重放核对）。
- `manual_sync`："立即同步"按钮 —— 日常对齐（含重放核对修复），结果供展示。
"""

from __future__ import annotations

from dataclasses import dataclass

from core.backup_restore import BackupError, export_backup, import_backup
from core.daily_sync import AlignResult, daily_align
from core.db import Database

# 口令最低长度（弱口令直接拒绝导出，避免不可恢复的废备份）
PASS_MIN_LENGTH = 8


def passphrase_strength(passphrase: str) -> str:
    """weak / medium / strong：长度 + 字符类别（数字/大小写/符号）。"""
    if len(passphrase) < PASS_MIN_LENGTH:
        return "weak"
    classes = 0
    for pred in (str.isdigit, str.islower, str.isupper, lambda c: not c.isalnum()):
        if any(pred(c) for c in passphrase):
            classes += 1
    if len(passphrase) >= 12 and classes >= 3:
        return "strong"
    if classes >= 2:
        return "medium"
    return "weak"


@dataclass(frozen=True)
class ImportResult:
    """导入结果：行数 + 恢复后对齐结果（sync 为空时 align 为 None）。"""

    rows: int
    align: AlignResult | None


def export_backup_flow(db: Database, passphrase: str, out_path) -> int:
    """口令校验 → 加密导出三表快照。返回导出行数。

    空口令/过短口令抛 `BackupError`（避免用户误导出无法恢复的废备份）。
    """
    if not passphrase or len(passphrase) < PASS_MIN_LENGTH:
        raise BackupError(f"备份口令至少 {PASS_MIN_LENGTH} 位")
    return export_backup(db, passphrase, out_path)


def import_backup_flow(
    db: Database, passphrase: str, in_path, sync=None
) -> ImportResult:
    """解密导入三表 → 恢复后对齐（date.sync 快照；不做重放修复）。

    `sync` 为 SyncManager 鸭子类型；为空则跳过对齐（仅导入）。
    对齐以 `mac_key=None` 调用 `daily_align`：恢复后亲密度以备份快照为准
    （spec §3.3.9），不依赖已清理/不可重放的旧事件流（spec 决策：备份不含
    会话密钥，换机重新配对后新会话密钥无法验证旧事件签名）。
    """
    rows = import_backup(db, passphrase, in_path)
    if sync is None:
        return ImportResult(rows=rows, align=None)
    return ImportResult(rows=rows, align=daily_align(sync, db, mac_key=None))


def manual_sync(sync, db: Database, mac_key: bytes) -> AlignResult:
    """"立即同步"：日常对齐（含重放核对修复），结果供 UI 展示。"""
    return daily_align(sync, db, mac_key)
