"""事件日志清理测试（data-consistency impl §4.2 / plan S3）。"""

from __future__ import annotations

import time

from core.db import init_core
from core.prune import prune_event_log
import core.prune as prune_mod

NOW = int(time.time())
DAY = 86400


def _insert(db, *, event_id, status="received", created_at=None, acked_at=None,
            expires_at=None, type="") -> None:
    db.execute(
        "INSERT OR IGNORE INTO events(event_id, seq, type, peer, payload_json, created_at, acked_at, status, expires_at) "
        "VALUES(?, NULL, ?, NULL, NULL, ?, ?, ?, ?)",
        (event_id, type, created_at or NOW, acked_at, status, expires_at),
    )


def _remaining(db) -> list[str]:
    return [str(r["event_id"]) for r in db.query_all("SELECT event_id FROM events")]


def test_received_older_than_retention_deleted(tmp_path) -> None:
    db = init_core(tmp_path)
    _insert(db, event_id="B:1", status="received", created_at=NOW - 100 * DAY)
    _insert(db, event_id="B:2", status="received", created_at=NOW - 10 * DAY)  # 保留期内
    n = prune_event_log(db)
    assert n == 1
    assert _remaining(db) == ["B:2"]
    db.close()


def test_received_exactly_cutoff_kept(tmp_path, monkeypatch) -> None:
    """边界：created_at == cutoff 不删（严格 <）。固定时钟避免跨秒抖动。"""
    monkeypatch.setattr(prune_mod.time, "time", lambda: NOW)
    db = init_core(tmp_path)
    cutoff = NOW - 90 * DAY
    _insert(db, event_id="B:1", status="received", created_at=cutoff)
    n = prune_event_log(db)
    assert n == 0
    assert _remaining(db) == ["B:1"]
    db.close()


def test_sent_acked_deleted_but_unacked_kept(tmp_path) -> None:
    db = init_core(tmp_path)
    # 已确认超期 → 删
    _insert(db, event_id="A:1", status="sent", created_at=NOW - 100 * DAY, acked_at=NOW - 99 * DAY)
    # 未确认未过期 → 留
    _insert(db, event_id="A:2", status="sent", created_at=NOW - 100 * DAY, acked_at=None, expires_at=NOW + 3600)
    n = prune_event_log(db)
    assert n == 1
    assert _remaining(db) == ["A:2"]
    db.close()


def test_failed_deleted(tmp_path) -> None:
    db = init_core(tmp_path)
    _insert(db, event_id="A:1", status="failed", created_at=NOW - 100 * DAY)
    _insert(db, event_id="A:2", status="failed", created_at=NOW - 10 * DAY)  # 保留期内
    n = prune_event_log(db)
    assert n == 1
    assert _remaining(db) == ["A:2"]
    db.close()


def test_unacked_but_expired_deleted(tmp_path) -> None:
    """未确认但已过期（expires_at < now）→ 视为失败可删。"""
    db = init_core(tmp_path)
    _insert(db, event_id="A:1", status="sent", created_at=NOW - 100 * DAY, acked_at=None, expires_at=NOW - 3600)
    n = prune_event_log(db)
    assert n == 1
    assert _remaining(db) == []
    db.close()


def test_batch_delete_limited(tmp_path) -> None:
    db = init_core(tmp_path)
    for i in range(5):
        _insert(db, event_id=f"B:{i}", status="received", created_at=NOW - 100 * DAY)
    # batch=2：分 3 批删完，最终全删
    n = prune_event_log(db, batch=2)
    assert n == 5
    assert _remaining(db) == []
    db.close()


def test_only_events_table_touched(tmp_path) -> None:
    db = init_core(tmp_path)
    db.execute(
        "INSERT INTO carries(id, direction, text, status, expires_at, created_at) "
        "VALUES('c1', 'out', 'x', 'failed', ?, ?)",
        (NOW + 3600, NOW - 100 * DAY),
    )
    _insert(db, event_id="B:1", status="received", created_at=NOW - 100 * DAY)
    prune_event_log(db)
    assert db.query_one("SELECT COUNT(*) AS n FROM carries")["n"] == 1  # 业务表零影响
    assert _remaining(db) == []
    db.close()


def test_pet_feed_exempt_from_prune(tmp_path) -> None:
    """pet.feed 事件保留完整历史（D17 重放收敛依赖），超期也不清理。"""
    db = init_core(tmp_path)
    _insert(db, event_id="A:1", status="received", created_at=NOW - 100 * DAY)      # 普通接收 → 删
    _insert(db, event_id="A:2", status="sent", created_at=NOW - 100 * DAY)          # 普通已发未确认 → 留
    _insert(db, event_id="A:3", status="received", created_at=NOW - 100 * DAY, type="pet.feed")  # 积分 → 豁免
    _insert(db, event_id="A:4", status="sent", created_at=NOW - 100 * DAY, type="pet.feed")      # 积分已发 → 豁免
    n = prune_event_log(db)
    assert n == 1
    assert _remaining(db) == ["A:2", "A:3", "A:4"]
    db.close()
