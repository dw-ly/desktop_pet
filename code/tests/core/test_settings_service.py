"""设置/备份服务层测试（data-consistency impl §4.6 / plan C1）。"""

from __future__ import annotations

import pytest

from sync.events import EventType
from sync.transport import ConnState

from core.db import init_core
from core.backup_restore import BackupError
from ui.settings_service import (
    PASS_MIN_LENGTH,
    ImportResult,
    export_backup_flow,
    import_backup_flow,
    manual_sync,
    passphrase_strength,
)

PASS = "正确口令需足够长并含多类字符1!"  # strong


class FakeSync:
    """SyncManager 鸭子类型（仅 connection_status/send）。"""

    def __init__(self, connected: bool = True) -> None:
        self._connected = connected
        self.sent: list[tuple] = []

    def connection_status(self) -> ConnState:
        return ConnState.CONNECTED if self._connected else ConnState.DISCONNECTED

    def send(self, type, payload, *, expires_at=None) -> None:
        self.sent.append((type, payload))


def _seed(db) -> None:
    db.execute(
        "INSERT INTO pet_state(key, value) VALUES('level', 5), ('intimacy', 88)"
    )
    db.execute(
        "INSERT INTO user_items(item_id, name, type, rarity, source, expire_at) "
        "VALUES('i1', '爱心气泡', 'costume', 'rare', 'B', NULL)"
    )
    db.execute(
        "INSERT INTO anniversaries(id, title, date, repeat, calendar, notify_days_before, updated_at) "
        "VALUES('a1', '在一起', '2026-08-05', 'yearly', 'solar', 3, 1783000000)"
    )


def test_passphrase_strength_levels() -> None:
    assert passphrase_strength("短") == "weak"
    assert passphrase_strength("12345678") == "weak"          # 仅数字一类
    assert passphrase_strength("abcdEFGH") == "medium"         # 大小写两类
    assert passphrase_strength("abcdEFGH1!xyz") == "strong"    # 12+ 位三类


def test_export_flow_rejects_weak_passphrase(tmp_path) -> None:
    db = init_core(tmp_path)
    with pytest.raises(BackupError):
        export_backup_flow(db, "", tmp_path / "x.bak")
    with pytest.raises(BackupError):
        export_backup_flow(db, "short", tmp_path / "x.bak")
    db.close()


def test_export_import_flow_roundtrip(tmp_path) -> None:
    db = init_core(tmp_path)
    _seed(db)
    bak = tmp_path / "pair.tuanzi.bak"
    assert export_backup_flow(db, PASS, bak) == 4  # 2 pet_state + 1 user_items + 1 anniversaries

    db2 = init_core(tmp_path / "new-device")
    res = import_backup_flow(db2, PASS, bak)  # sync=None：仅导入
    assert isinstance(res, ImportResult)
    assert res.rows == 4
    assert res.align is None
    assert db2.query_one("SELECT value FROM pet_state WHERE key='intimacy'")["value"] == 88
    assert db2.query_one("SELECT COUNT(*) AS n FROM user_items")["n"] == 1
    assert db2.query_one("SELECT COUNT(*) AS n FROM anniversaries")["n"] == 1
    db.close()
    db2.close()


def test_import_flow_aligns_snapshot_when_connected(tmp_path) -> None:
    """恢复后对齐：mac_key=None 仅发 date.sync 快照，不做重放修复。"""
    db = init_core(tmp_path)
    _seed(db)
    bak = tmp_path / "pair.tuanzi.bak"
    export_backup_flow(db, PASS, bak)

    db2 = init_core(tmp_path / "new-device")
    sync = FakeSync(connected=True)
    res = import_backup_flow(db2, PASS, bak, sync=sync)
    assert res.align.value == "ok"
    assert len(sync.sent) == 1
    assert sync.sent[0][0] is EventType.DATE_SYNC
    # 备份快照为准：亲密度不被清空（无事件可重放也保持 88）
    assert db2.query_one("SELECT value FROM pet_state WHERE key='intimacy'")["value"] == 88
    db.close()
    db2.close()


def test_import_flow_deferred_when_disconnected(tmp_path) -> None:
    db = init_core(tmp_path)
    _seed(db)
    bak = tmp_path / "pair.tuanzi.bak"
    export_backup_flow(db, PASS, bak)

    db2 = init_core(tmp_path / "new-device")
    res = import_backup_flow(db2, PASS, bak, sync=FakeSync(connected=False))
    assert res.align.value == "deferred"  # 未连接推迟，数据已导入
    assert db2.query_one("SELECT value FROM pet_state WHERE key='intimacy'")["value"] == 88
    db.close()
    db2.close()


def test_manual_sync_sends_snapshot(tmp_path) -> None:
    db = init_core(tmp_path)
    db.execute("INSERT INTO pet_state(key, value) VALUES('intimacy', 0)")
    sync = FakeSync(connected=True)
    assert manual_sync(sync, db, b"k" * 32).value == "ok"
    assert sync.sent[0][0] is EventType.DATE_SYNC
    db.close()


def test_manual_sync_deferred_when_offline(tmp_path) -> None:
    db = init_core(tmp_path)
    assert manual_sync(FakeSync(connected=False), db, b"k" * 32).value == "deferred"
    db.close()
