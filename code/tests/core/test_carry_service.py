"""带话服务测试（carry-message impl §4.6）。"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from core.carry import CarryConfig, CarryService
from core.carry_store import CarryRecord, CarryStatus, CarryStore
from core.db import init_core
from sync.events import EventType, Message


class FakeSync:
    """CarryService 依赖的 sync 门面（鸭子类型，仅记录 send 调用）。"""

    def __init__(self) -> None:
        self.sent: list[tuple[EventType, dict, int | None]] = []

    def send(self, type, payload, *, expires_at=None) -> None:
        self.sent.append((type, payload, expires_at))

    def add_handler(self, type, handler) -> None:
        pass


@pytest.fixture
def env(tmp_path):
    db = init_core(tmp_path)
    store = CarryStore(db)
    sync = FakeSync()
    confirms: list[CarryRecord] = []
    statuses: list[CarryRecord] = []
    hides: list[str] = []
    svc = CarryService(
        store,
        sync,
        CarryConfig(),
        on_confirm=confirms.append,
        on_status=statuses.append,
        on_hide=hides.append,
    )
    yield SimpleNamespace(
        db=db, store=store, sync=sync, svc=svc,
        confirms=confirms, statuses=statuses, hides=hides,
    )
    db.close()


def _msg(carry_id: str, ts: int, *, type: str = "carry.ack", **extra) -> Message:
    return Message(
        v=1, type=type, from_id="peerB", seq=1, ts=ts,
        payload={"carryId": carry_id, **extra},
    )


def _send_and_get(env, text: str = "告诉TA晚安") -> str:
    """propose + confirm_send，返回发送记录 id。"""
    assert env.svc.propose(text) is True
    rid = env.confirms[-1].id
    env.svc.confirm_send(rid)
    return rid


# ------------------------------------------------------------------ #
# 发送侧
# ------------------------------------------------------------------ #


def test_propose_hit_enters_confirm(env):
    ok = env.svc.propose("告诉TA晚安")
    assert ok is True
    assert len(env.confirms) == 1
    rec = env.confirms[0]
    assert rec.direction == "out"
    assert rec.status == CarryStatus.DRAFT
    assert rec.text == "晚安"
    assert env.store.get(rec.id) is not None


def test_propose_miss_no_record(env):
    ok = env.svc.propose("今天好热")
    assert ok is False
    assert env.confirms == []
    assert env.db.query_one("SELECT COUNT(*) AS n FROM carries")["n"] == 0


def test_confirm_only_from_draft(env):
    rec = env.store.create_outgoing("晚安", int(time.time()) + 3600)
    env.store.mark_sent(rec.id, int(time.time()))
    env.svc.confirm_send(rec.id)  # 已 sent，非 draft → 不发送
    assert env.sync.sent == []


def test_confirm_send_payload(env):
    rid = _send_and_get(env)
    assert env.store.get(rid).status == CarryStatus.SENT
    assert len(env.sync.sent) == 1
    etype, payload, exp = env.sync.sent[0]
    assert etype == EventType.MSG_CARRY
    assert payload["carryId"] == rid
    assert payload["text"] == "晚安"
    assert payload["mood"] is None  # 未注入情绪读取 → null
    assert payload["expireAt"] == env.store.get(rid).expires_at
    assert exp == payload["expireAt"]


# ------------------------------------------------------------------ #
# 撤回
# ------------------------------------------------------------------ #


def test_revoke_in_window(env):
    rid = _send_and_get(env)
    sent_at = env.store.get(rid).sent_at
    env.svc.revoke(rid)
    assert env.store.get(rid).status == CarryStatus.REVOKED
    etype, payload, _ = env.sync.sent[-1]
    assert etype == EventType.CARRY_REVOKE
    assert payload["sentAt"] == sent_at


def test_revoke_out_of_window(env):
    rid = _send_and_get(env)
    env.db.execute(
        "UPDATE carries SET sent_at=? WHERE id=?", (int(time.time()) - 200, rid)
    )
    env.svc.revoke(rid)
    assert env.store.get(rid).status == CarryStatus.SENT
    assert all(t != EventType.CARRY_REVOKE for t, _, _ in env.sync.sent)


def test_revoke_delivered_rejected(env):
    rid = _send_and_get(env)
    env.store.mark_delivered(rid, int(time.time()))
    env.svc.revoke(rid)
    assert env.store.get(rid).status == CarryStatus.DELIVERED
    assert all(t != EventType.CARRY_REVOKE for t, _, _ in env.sync.sent)


# ------------------------------------------------------------------ #
# ack 处理（发送端）
# ------------------------------------------------------------------ #


def test_ack_idempotent(env):
    rid = _send_and_get(env)
    env.svc.on_ack(_msg(rid, int(time.time()), ackedAt=int(time.time())))
    assert env.store.get(rid).status == CarryStatus.DELIVERED
    env.svc.on_ack(_msg(rid, int(time.time()), ackedAt=int(time.time())))
    assert env.store.get(rid).status == CarryStatus.DELIVERED
    delivered = [r for r in env.statuses if r.status == CarryStatus.DELIVERED]
    assert len(delivered) == 1  # 重复 ack 不重复通知


def test_ack_after_revoke_rollback(env):
    rid = _send_and_get(env)
    env.svc.revoke(rid)
    assert env.store.get(rid).status == CarryStatus.REVOKED
    env.svc.on_ack(_msg(rid, int(time.time()), ackedAt=int(time.time())))
    assert env.store.get(rid).status == CarryStatus.DELIVERED  # D3 已读优先


# ------------------------------------------------------------------ #
# revoke 处理（接收端）
# ------------------------------------------------------------------ #


def test_revoke_ignored_when_delivered(env):
    rid = "recv-rid-1"
    env.store.insert_incoming(rid, "hi", int(time.time()) + 3600)
    env.store.mark_delivered(rid, int(time.time()))
    env.svc.on_revoke(_msg(rid, int(time.time()), type="carry.revoke"))
    assert env.store.get(rid).status == CarryStatus.DELIVERED
    assert env.hides == []


def test_revoke_effective_receiver(env):
    rid = "recv-rid-2"
    env.store.insert_incoming(rid, "hi", int(time.time()) + 3600)
    env.svc.on_revoke(_msg(rid, int(time.time()), type="carry.revoke"))
    assert env.store.get(rid).status == CarryStatus.REVOKED
    assert env.hides == [rid]  # notify.hide 被调


# ------------------------------------------------------------------ #
# 过期扫描
# ------------------------------------------------------------------ #


def test_scan_expired(env):
    rid = _send_and_get(env)
    env.db.execute(
        "UPDATE carries SET expires_at=? WHERE id=?", (int(time.time()) - 1, rid)
    )
    env.svc.scan_expired(int(time.time()))
    assert env.store.get(rid).status == CarryStatus.FAILED
    assert env.statuses[-1].status == CarryStatus.FAILED


def test_scan_expired_boundary(env):
    rid = _send_and_get(env)
    now = int(time.time())
    env.db.execute("UPDATE carries SET expires_at=? WHERE id=?", (now, rid))
    env.svc.scan_expired(now)
    assert env.store.get(rid).status == CarryStatus.SENT  # 边界未到，不置 failed


# ------------------------------------------------------------------ #
# 勿扰时段
# ------------------------------------------------------------------ #


def test_is_dnd():
    cfg = CarryConfig(dnd_start="22:00", dnd_end="08:00")

    def local_ts(h: int, m: int) -> int:
        return int(time.mktime((2026, 8, 4, h, m, 0, 0, 0, -1)))

    assert cfg.is_dnd(local_ts(23, 0)) is True   # 跨午夜区间内
    assert cfg.is_dnd(local_ts(8, 0)) is False   # 结束边界不含
    assert cfg.is_dnd(local_ts(21, 59)) is False  # 起始前
    assert CarryConfig().is_dnd(local_ts(23, 0)) is False  # 未配置 → 恒 False
