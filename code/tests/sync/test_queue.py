"""离线队列与去重测试（data-consistency impl §4.1 / plan S1+S2）。

收编后 SyncQueue 基于统一 core.db（Database），构造为 SyncQueue(db, my_peer_id)；
含旧 sync_queue.db 数据迁移 migrate_legacy_queue 用例。
"""

import asyncio
import sqlite3
import time
from pathlib import Path

from core.db import init_core
from sync.events import Message
from sync.queue import LEGACY_MIGRATION_KEY, SyncQueue, migrate_legacy_queue


def _make_queue(tmp_path, my_peer_id: str = "A") -> tuple:
    db = init_core(tmp_path)
    return db, SyncQueue(db, my_peer_id)


def _msg(seq: int, payload: dict, from_id: str = "B") -> Message:
    return Message(v=1, type="msg.carry", from_id=from_id, seq=seq, ts=int(time.time()), payload=payload)


def test_record_sent_creates_sent_row(tmp_path) -> None:
    """在线直发成功后落 events 表（status='sent'，D17 完整事件日志）。"""
    db, q = _make_queue(tmp_path)
    q.record_sent("B", 7, "pet.feed", {"delta": 3, "reason": "feed", "ts": 1, "sig": "s"})
    row = db.query_one("SELECT * FROM events WHERE event_id='A:7'")
    assert row["status"] == "sent"
    assert row["peer"] == "B"
    assert row["type"] == "pet.feed"
    assert "delta" in str(row["payload_json"])
    # 与 enqueue 的 pending 语义区分：record_sent 不会被 flush 重发
    sent: list[int] = []

    async def cb(m: Message) -> None:
        sent.append(m.seq)

    asyncio.run(q.flush("B", cb))
    assert sent == []  # sent 行不补发
    db.close()


def test_enqueue_flush_ordered(tmp_path) -> None:
    db, q = _make_queue(tmp_path)
    q.enqueue("B", 1, "msg.carry", {"n": 1})
    q.enqueue("B", 3, "msg.carry", {"n": 3})
    q.enqueue("B", 2, "msg.carry", {"n": 2})
    sent: list[int] = []

    async def cb(m: Message) -> None:
        sent.append(m.seq)

    asyncio.run(q.flush("B", cb))
    assert sent == [1, 2, 3]
    db.close()


def test_flush_idempotent_no_resend(tmp_path) -> None:
    db, q = _make_queue(tmp_path)
    q.enqueue("B", 1, "msg.carry", {"n": 1})
    sent: list[int] = []

    async def cb(m: Message) -> None:
        sent.append(m.seq)

    asyncio.run(q.flush("B", cb))
    asyncio.run(q.flush("B", cb))  # 第二次 flush 不应重复发送
    assert sent == [1]
    db.close()


def test_expired_marked_failed(tmp_path) -> None:
    db, q = _make_queue(tmp_path)
    q.enqueue("B", 1, "msg.carry", {"n": 1}, expires_at=int(time.time()) - 10)
    sent: list[int] = []

    async def cb(m: Message) -> None:
        sent.append(m.seq)

    errors = asyncio.run(q.flush("B", cb))
    assert sent == []
    assert errors == []
    row = db.query_one("SELECT status FROM events WHERE event_id='A:1'")
    assert row["status"] == "failed"
    db.close()


def test_failed_send_stays_pending(tmp_path) -> None:
    db, q = _make_queue(tmp_path)
    q.enqueue("B", 1, "msg.carry", {"n": 1})
    attempts = 0

    async def cb(m: Message) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("模拟发送失败")

    errors = asyncio.run(q.flush("B", cb))
    assert len(errors) == 1
    # 第二次 flush 重试成功
    errors2 = asyncio.run(q.flush("B", cb))
    assert errors2 == []
    assert attempts == 2
    db.close()


def test_receive_dedup_single_delivery(tmp_path) -> None:
    db, q = _make_queue(tmp_path)
    delivered: list[str] = []

    def deliver(m: Message) -> None:
        delivered.append(m.seq)

    m = _msg(5, {"n": 5})
    assert q.receive(m, deliver) is True
    assert q.receive(m, deliver) is False   # 重复丢弃
    assert delivered == [5]
    db.close()


def test_receive_same_seq_different_peer(tmp_path) -> None:
    db, q = _make_queue(tmp_path)
    delivered: list[str] = []

    def deliver(m: Message) -> None:
        delivered.append(f"{m.from_id}:{m.seq}")

    assert q.receive(_msg(5, {}), deliver) is True
    m2 = _msg(5, {}, from_id="C")
    assert q.receive(m2, deliver) is True
    assert delivered == ["B:5", "C:5"]
    db.close()


def test_receive_deliver_failure_rolls_back(tmp_path) -> None:
    db, q = _make_queue(tmp_path)

    def boom(m: Message) -> None:
        raise RuntimeError("投递失败")

    m = _msg(7, {})
    try:
        q.receive(m, boom)
        assert False, "应抛出 RuntimeError"
    except RuntimeError:
        pass
    # 回滚后应能再次投递（events 无残留）
    delivered: list[int] = []

    def deliver(m: Message) -> None:
        delivered.append(m.seq)

    assert q.receive(m, deliver) is True
    assert delivered == [7]
    db.close()


# ------------------------------------------------------------------ #
# 旧 sync_queue.db 数据迁移（migrate_legacy_queue）
# ------------------------------------------------------------------ #

_OLD_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  peer_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  expires_at INTEGER,
  created_at INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
);
CREATE UNIQUE INDEX idx_outbox_seq ON outbox(peer_id, seq);
CREATE TABLE seen_events (
  from_peer TEXT NOT NULL,
  seq INTEGER NOT NULL,
  received_at INTEGER NOT NULL,
  PRIMARY KEY (from_peer, seq)
);
"""


def _make_legacy_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_SCHEMA)
    conn.execute("INSERT INTO meta(key, value) VALUES('B:next_seq', '10')")
    conn.execute(
        "INSERT INTO outbox(peer_id, seq, type, payload_json, expires_at, created_at, status) "
        "VALUES('B', 1, 'msg.carry', '{\"n\":1}', NULL, 100, 'pending')"
    )
    conn.execute(
        "INSERT INTO outbox(peer_id, seq, type, payload_json, expires_at, created_at, status) "
        "VALUES('B', 2, 'msg.carry', '{\"n\":2}', 1000, 200, 'sent')"
    )
    conn.execute("INSERT INTO seen_events(from_peer, seq, received_at) VALUES('B', 5, 300)")
    conn.commit()
    conn.close()


def test_migrate_legacy_moves_data(tmp_path) -> None:
    old_path = str(tmp_path / "sync_queue.db")
    _make_legacy_db(old_path)
    db = init_core(tmp_path / "core")  # 独立目录，core.db 与旧库路径不同
    n = migrate_legacy_queue(db, old_path, my_peer_id="A")
    assert n == 3  # 2 outbox + 1 seen

    # outbox → events（发送侧，event_id 前缀本端）
    r1 = db.query_one("SELECT * FROM events WHERE event_id='A:1'")
    assert r1["status"] == "pending"
    assert r1["peer"] == "B"
    assert r1["type"] == "msg.carry"
    r2 = db.query_one("SELECT * FROM events WHERE event_id='A:2'")
    assert r2["status"] == "sent"
    # seen → events（接收侧，type=''）
    r3 = db.query_one("SELECT * FROM events WHERE event_id='B:5'")
    assert r3["status"] == "received"
    assert r3["type"] == ""
    assert r3["created_at"] == 300
    # meta → kv（seq 游标保留）
    assert db.query_one("SELECT value FROM kv WHERE key='B:next_seq'")["value"] == "10"
    assert db.query_one("SELECT 1 FROM kv WHERE key=?", (LEGACY_MIGRATION_KEY,)) is not None
    db.close()


def test_migrate_legacy_idempotent(tmp_path) -> None:
    old_path = str(tmp_path / "sync_queue.db")
    _make_legacy_db(old_path)
    db = init_core(tmp_path / "core")
    assert migrate_legacy_queue(db, old_path, my_peer_id="A") == 3
    assert migrate_legacy_queue(db, old_path, my_peer_id="A") == 0  # 幂等
    assert db.query_one("SELECT COUNT(*) AS n FROM events")["n"] == 3
    db.close()


def test_migrate_legacy_missing_db(tmp_path) -> None:
    db = init_core(tmp_path / "core")
    assert migrate_legacy_queue(db, str(tmp_path / "not_exist.db"), my_peer_id="A") == 0
    assert db.query_one("SELECT 1 FROM kv WHERE key=?", (LEGACY_MIGRATION_KEY,)) is not None
    db.close()


def test_migrate_legacy_corrupt_db(tmp_path) -> None:
    bad = tmp_path / "sync_queue.db"
    bad.write_bytes(b"this is not a sqlite db")
    db = init_core(tmp_path / "core")
    # 损坏旧库：降级跳过（warning），不阻塞、无半迁移
    assert migrate_legacy_queue(db, str(bad), my_peer_id="A") == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM events")["n"] == 0
    assert db.query_one("SELECT 1 FROM kv WHERE key=?", (LEGACY_MIGRATION_KEY,)) is not None
    db.close()
