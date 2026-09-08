"""带话接收编排测试（carry-message impl §5.3）。"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from core.carry import CarryConfig, CarryService
from core.carry_receive import CarryReceiver
from core.carry_store import CarryRecord, CarryStatus, CarryStore
from core.db import init_core
from sync.events import EventType, Message


class FakeSync:
    def __init__(self) -> None:
        self.sent: list[tuple[EventType, dict, int | None]] = []

    def send(self, type, payload, *, expires_at=None) -> None:
        self.sent.append((type, payload, expires_at))


class FakeNotify:
    """UI 播报接口（鸭子类型）。"""

    def __init__(self) -> None:
        self.shown: list[tuple[CarryRecord, object]] = []

    def show_carry(self, rec: CarryRecord, on_ack) -> None:
        self.shown.append((rec, on_ack))


@pytest.fixture
def env(tmp_path):
    db = init_core(tmp_path)
    store = CarryStore(db)
    sync = FakeSync()
    svc = CarryService(
        store, sync, CarryConfig(),
        on_confirm=lambda r: None, on_status=lambda r: None,
    )
    notify = FakeNotify()
    dnd = {"on": False}
    recv = CarryReceiver(store, svc, notify, is_dnd=lambda: dnd["on"])
    yield SimpleNamespace(
        db=db, store=store, sync=sync, svc=svc, recv=recv,
        notify=notify, dnd=dnd,
    )
    db.close()


def _carry_msg(carry_id: str, text: str = "今晚早点睡", ts: int | None = None,
               expires_at: int | None = None) -> Message:
    ts = ts if ts is not None else int(time.time())
    return Message(
        v=1, type="msg.carry", from_id="peerA", seq=1, ts=ts,
        payload={"carryId": carry_id, "text": text, "expireAt": expires_at or ts + 3600},
    )


def test_on_msg_normal_broadcast(env):
    msg = _carry_msg("r1", text="今晚早点睡")
    env.recv.on_msg(msg)
    assert len(env.notify.shown) == 1
    rec = env.notify.shown[0][0]
    assert rec.status == CarryStatus.SENT
    assert rec.direction == "in"
    assert env.store.get("r1").text == "今晚早点睡"
    # 点击"知道了" → 回 ack
    env.notify.shown[0][1]()
    assert env.store.get("r1").status == CarryStatus.DELIVERED
    assert env.sync.sent[-1][0] == EventType.CARRY_ACK
    assert env.sync.sent[-1][1]["auto"] is False


def test_on_msg_duplicate_idempotent(env):
    msg = _carry_msg("r1")
    env.recv.on_msg(msg)
    env.recv.on_msg(msg)
    assert len(env.notify.shown) == 1  # 仅播报一次
    assert env.db.query_one("SELECT COUNT(*) AS n FROM carries")["n"] == 1


def test_on_msg_dnd_silent(env):
    env.dnd["on"] = True
    env.recv.on_msg(_carry_msg("r1"))
    assert env.notify.shown == []
    rec = env.store.get("r1")
    assert rec is not None and rec.status == CarryStatus.SENT  # 静默入库，稍后看可查


def test_auto_ack(env):
    now = int(time.time())
    env.store.insert_incoming("r1", "hi", now + 3600)
    env.db.execute("UPDATE carries SET created_at=? WHERE id=?", (now - 200, "r1"))
    env.recv.scan_auto_ack()
    assert env.store.get("r1").status == CarryStatus.DELIVERED
    etype, payload, _ = env.sync.sent[-1]
    assert etype == EventType.CARRY_ACK
    assert payload["auto"] is True


def test_auto_ack_skips_manual_acked(env):
    now = int(time.time())
    env.store.insert_incoming("r1", "hi", now + 3600)
    env.store.mark_delivered("r1", now - 100)  # 已手动确认
    env.recv.scan_auto_ack()
    assert env.sync.sent == []  # 跳过


def test_revoke_removes_from_later_list(env):
    now = int(time.time())
    env.store.insert_incoming("r1", "hi", now + 3600)
    assert [r.id for r in env.store.unacked_incoming(now + 1)] == ["r1"]  # 在稍后看中
    env.svc.on_revoke(
        Message(
            v=1, type="carry.revoke", from_id="peerA", seq=2, ts=now,
            payload={"carryId": "r1", "sentAt": now},
        )
    )
    assert env.store.get("r1").status == CarryStatus.REVOKED
    assert env.store.unacked_incoming(now + 1) == []  # 移出稍后看
