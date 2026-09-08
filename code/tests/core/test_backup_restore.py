"""换机/重装恢复测试（data-consistency impl §4.5 / plan S6）。"""

from __future__ import annotations

import os

import pytest

from core.backup_restore import BackupError, export_backup, import_backup
from core.db import init_core

PASS = "正确口令，换机后旧备份仍可解"


def _seed(db) -> None:
    db.execute(
        "INSERT INTO pet_state(key, value) VALUES('level', 5), ('exp', 2500), ('intimacy', 88)"
    )
    db.execute(
        "INSERT INTO user_items(item_id, name, type, rarity, source, expire_at) "
        "VALUES('i1', '爱心气泡', 'costume', 'rare', 'B', NULL), "
        "('i2', '陪伴动画', 'action', 'common', 'B', 1800000000)"
    )
    db.execute(
        "INSERT INTO anniversaries(id, title, date, repeat, calendar, notify_days_before, updated_at) "
        "VALUES('a1', '在一起', '2026-08-05', 'yearly', 'solar', 3, 1783000000)"
    )


def _snapshot(db) -> dict:
    return {
        "pet_state": [dict(r) for r in db.query_all("SELECT * FROM pet_state")],
        "user_items": [dict(r) for r in db.query_all("SELECT * FROM user_items")],
        "anniversaries": [dict(r) for r in db.query_all("SELECT * FROM anniversaries")],
    }


def test_export_import_roundtrip(tmp_path) -> None:
    db = init_core(tmp_path)
    _seed(db)
    bak = tmp_path / "pair.tuanzi.bak"
    assert export_backup(db, PASS, bak) == 6

    # 模拟换机：全新 data_dir
    db2 = init_core(tmp_path / "new-device")
    assert _snapshot(db2) == {
        "pet_state": [], "user_items": [], "anniversaries": [],
    }
    assert import_backup(db2, PASS, bak) == 6
    assert _snapshot(db2) == _snapshot(db)
    db.close()
    db2.close()


def test_wrong_passphrase_keeps_db_unchanged(tmp_path) -> None:
    db = init_core(tmp_path)
    _seed(db)
    before = _snapshot(db)
    bak = tmp_path / "pair.tuanzi.bak"
    export_backup(db, PASS, bak)

    db2 = init_core(tmp_path / "new-device")
    with pytest.raises(BackupError):
        import_backup(db2, "错误口令", bak)
    assert _snapshot(db2) == {
        "pet_state": [], "user_items": [], "anniversaries": [],
    }  # 现有库零影响
    db.close()
    db2.close()


def test_not_a_backup_file_raises(tmp_path) -> None:
    db = init_core(tmp_path)
    bogus = tmp_path / "bogus.bak"
    bogus.write_bytes(b"plain text, not tuanzi backup")
    with pytest.raises(BackupError):
        import_backup(db, PASS, bogus)
    db.close()


def test_import_does_not_touch_other_tables(tmp_path) -> None:
    db = init_core(tmp_path)
    _seed(db)
    bak = tmp_path / "pair.tuanzi.bak"
    export_backup(db, PASS, bak)

    db2 = init_core(tmp_path / "new-device")
    db2.execute(
        "INSERT INTO events(event_id, seq, type, peer, payload_json, created_at, acked_at, status, expires_at) "
        "VALUES('A:1', NULL, 'msg.carry', 'B', '{}', 100, NULL, 'received', NULL)"
    )
    db2.execute(
        "INSERT INTO carries(id, direction, text, status, expires_at, created_at) "
        "VALUES('c1', 'out', 'x', 'sent', 200, 100)"
    )
    import_backup(db2, PASS, bak)
    # 导入只覆盖三表；events / carries 原样保留
    assert db2.query_one("SELECT COUNT(*) AS n FROM events")["n"] == 1
    assert db2.query_one("SELECT COUNT(*) AS n FROM carries")["n"] == 1
    db.close()
    db2.close()


def test_export_empty_tables_ok(tmp_path) -> None:
    db = init_core(tmp_path)
    bak = tmp_path / "empty.tuanzi.bak"
    assert export_backup(db, PASS, bak) == 0
    db2 = init_core(tmp_path / "new-device")
    assert import_backup(db2, PASS, bak) == 0
    db.close()
    db2.close()


def test_truncated_backup_raises(tmp_path) -> None:
    db = init_core(tmp_path)
    bak = tmp_path / "pair.tuanzi.bak"
    export_backup(db, PASS, bak)
    # 截断到只剩 magic
    truncated = tmp_path / "truncated.bak"
    truncated.write_bytes(bak.read_bytes()[:8])
    with pytest.raises(BackupError):
        import_backup(db, PASS, truncated)
    db.close()
