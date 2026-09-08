"""带话存储层测试（carry-message impl §3.4）。"""

from __future__ import annotations

import threading
import time

import pytest

from core.carry_store import CarryRecord, CarryStatus, CarryStore
from core.db import init_core


@pytest.fixture
def store(tmp_path):
    db = init_core(tmp_path)  # 建 core.db + 跑迁移（carries 表由 0001 建表）
    yield CarryStore(db)
    db.close()


def _sent_out(store: CarryStore, text: str = "hello", expires_at: int | None = None) -> CarryRecord:
    """构造一条 out + sent 记录（create_outgoing + mark_sent）。"""
    rec = store.create_outgoing(text, expires_at or int(time.time()) + 3600)
    assert store.mark_sent(rec.id, int(time.time()))
    return rec


# ------------------------------------------------------------------ #
# 建库建表
# ------------------------------------------------------------------ #


def test_carries_table_ready(tmp_path):
    """统一库 core.db 中 carries 表与索引由 0001 基线建好，本模块不建表。"""
    db = init_core(tmp_path)
    try:
        assert db.table_exists("carries")
        idx = db.query_one(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_carries_out_status'"
        )
        assert idx is not None
    finally:
        db.close()


# ------------------------------------------------------------------ #
# 写入
# ------------------------------------------------------------------ #


def test_create_outgoing(store):
    rec = store.create_outgoing("晚安", int(time.time()) + 86400)
    assert rec.status == CarryStatus.DRAFT
    assert rec.direction == "out"
    assert len(rec.id) == 32  # uuid4 hex


def test_insert_incoming_idempotent(store):
    rid = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    exp = int(time.time()) + 86400
    first = store.insert_incoming(rid, "今晚早点睡", exp)
    assert first is not None
    assert first.status == CarryStatus.SENT
    assert first.direction == "in"
    # 重复投递：返回 None，不覆盖
    again = store.insert_incoming(rid, "覆盖文本", exp)
    assert again is None
    rec = store.get(rid)
    assert rec.text == "今晚早点睡"  # 原记录未被覆盖


# ------------------------------------------------------------------ #
# 状态流转
# ------------------------------------------------------------------ #


def test_legal_transition_chain(store):
    rec = store.create_outgoing("早安", int(time.time()) + 86400)
    assert store.mark_sent(rec.id, int(time.time()))
    assert store.mark_delivered(rec.id, int(time.time()))
    assert store.get(rec.id).status == CarryStatus.DELIVERED


def test_invalid_delivered_revoke(store):
    rec = _sent_out(store)
    assert store.mark_delivered(rec.id, int(time.time()))
    assert store.mark_revoked(rec.id, int(time.time())) is False  # delivered→revoked 非法
    assert store.get(rec.id).status == CarryStatus.DELIVERED


def test_invalid_final_state(store):
    rec = _sent_out(store)
    assert store.mark_failed(rec.id)
    assert store.mark_sent(rec.id, int(time.time())) is False  # failed→sent 非法
    assert store.get(rec.id).status == CarryStatus.FAILED


def test_rollback_revoked_to_delivered(store):
    """D3：revoked 后 ack 后到 → 回滚 delivered。"""
    rec = _sent_out(store)
    assert store.mark_revoked(rec.id, int(time.time()))
    assert store.rollback_revoked_to_delivered(rec.id, int(time.time()))
    rec2 = store.get(rec.id)
    assert rec2.status == CarryStatus.DELIVERED
    assert rec2.read_at is not None
    # 非 revoked 状态回滚拒绝
    assert store.rollback_revoked_to_delivered(rec.id, int(time.time())) is False


# ------------------------------------------------------------------ #
# 扫描查询
# ------------------------------------------------------------------ #


def test_pending_expired(store):
    now = int(time.time())
    expired = _sent_out(store, expires_at=now - 1)
    _sent_out(store, expires_at=now + 3600)  # 未过期
    _sent_out(store, expires_at=now)  # 边界：严格小于才过期
    got = store.pending_expired(now)
    assert [r.id for r in got] == [expired.id]


def test_unacked_incoming(store):
    now = int(time.time())
    for i in range(3):
        store.insert_incoming(f"in-{i}", f"msg{i}", now + 3600)
    # in-0/in-1 超截止（应返回），in-2 未超截止（应排除）
    store._db.execute(
        "UPDATE carries SET created_at=? WHERE id IN ('in-0','in-1')", (now - 100,)
    )
    store._db.execute(
        "UPDATE carries SET created_at=? WHERE id='in-2'", (now + 100,)
    )
    got = store.unacked_incoming(now)
    assert sorted(r.id for r in got) == ["in-0", "in-1"]


# ------------------------------------------------------------------ #
# 并发与原子性
# ------------------------------------------------------------------ #


def test_concurrent_mark(store):
    ids = []
    for i in range(100):
        rec = _sent_out(store, f"msg{i}")
        ids.append(rec.id)
    errors: list[Exception] = []

    def worker(start: int) -> None:
        try:
            for i in range(start, start + 50):
                assert store.mark_delivered(ids[i], int(time.time()))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(0,)),
        threading.Thread(target=worker, args=(50,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    for cid in ids:
        assert store.get(cid).status == CarryStatus.DELIVERED


def test_insert_incoming_rollback(store):
    """事务内 insert_incoming 后回滚 → 表中无半条记录。"""
    rid = "rollback-rollback-rollback-x"
    with pytest.raises(RuntimeError):
        with store._db.transaction():
            store.insert_incoming(rid, "不要残留", int(time.time()) + 3600)
            raise RuntimeError("boom")
    assert store.get(rid) is None
