"""每日结算与灰色期状态机测试（pet-growth impl §6.3 / plan S4）。"""

from __future__ import annotations

import itertools
from datetime import datetime

import pytest

from core.daily_sync import handle_peer_snapshot
from core.db import init_core
from core.pet_growth import PetGrowth, set_anniversary_hook
from core.pet_streak import PetStreak, SettleStatus
from sync.events import EventType
from sync.transport import ConnState

MAC_KEY = b"k" * 32
DAY1 = int(datetime(2026, 8, 1, 12).timestamp())  # 结算时刻（当日 12:00）
DAY2 = int(datetime(2026, 8, 2, 12).timestamp())
DAY3 = int(datetime(2026, 8, 3, 12).timestamp())
DAY4 = int(datetime(2026, 8, 4, 12).timestamp())

_counter = itertools.count()


class FakeSync:
    def __init__(self) -> None:
        self.calls: list[tuple[EventType, dict]] = []

    def send(self, type_, payload: dict, **kwargs) -> None:
        self.calls.append((type_, payload))

    def connection_status(self) -> ConnState:
        return ConnState.CONNECTED


def _make(tmp_path):
    db = init_core(tmp_path)
    sync = FakeSync()
    growth = PetGrowth(db, sync, MAC_KEY)
    streak = PetStreak(db, growth)
    return db, sync, growth, streak


def _seed_activity(db, my_id: str, ts: int, *, local: bool = True, peer: bool = True) -> None:
    """在 events 表播种当日活动：local → status='sent'；peer → status='received'。"""
    n = next(_counter)
    if local:
        db.execute(
            "INSERT INTO events(event_id, seq, type, peer, payload_json, created_at, status) "
            "VALUES(?, ?, 'pet.feed', 'peer', '{}', ?, 'sent')",
            (f"{my_id}:{n}0", n * 10, ts),
        )
    if peer:
        db.execute(
            "INSERT INTO events(event_id, seq, type, peer, payload_json, created_at, status) "
            "VALUES(?, ?, 'pet.feed', 'peer', '{}', ?, 'received')",
            (f"peer:{n}1", n * 10 + 1, ts),
        )


def _preseed(db, *, streak: int = 0, grace: str | None = None,
             grace_left: int | None = None, grace_progress: int = 0) -> None:
    db.execute(
        "INSERT INTO pet_state(key, value) VALUES('streak_days', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (streak,),
    )
    if grace_left is not None:
        db.execute(
            "INSERT INTO pet_state(key, value) VALUES('grace_left', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (grace_left,),
        )
    if grace_progress:
        db.execute(
            "INSERT INTO pet_state(key, value) VALUES('grace_progress', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (grace_progress,),
        )
    if grace is not None:
        db.execute(
            "INSERT INTO kv(key, value) VALUES('pet:grace_status', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (grace,),
        )


def _intimacy(db) -> int:
    row = db.query_one("SELECT value FROM pet_state WHERE key='intimacy'")
    return int(row["value"]) if row else 0


# --------------------------------------------------------------------------- #

def test_normal_both_active_streak_award(tmp_path) -> None:
    db, sync, _, streak = _make(tmp_path)
    _seed_activity(db, "B", DAY1, local=True, peer=True)
    out = streak.settle("B", "A", now=DAY1)  # "B" > "A" → B 为生成方
    assert out.status == SettleStatus.OK
    assert out.streak == 1
    assert out.grace_status == "normal"
    assert out.both_active is True
    assert out.event_sent is True
    assert out.awarded_delta == 2  # streak 1 × 2
    assert _intimacy(db) == 2
    assert sync.calls[0][0] == EventType.PET_FEED
    assert sync.calls[0][1]["reason"] == "streak"
    assert sync.calls[0][1]["delta"] == 2
    db.close()


def test_single_active_transitions_to_grace(tmp_path) -> None:
    db, sync, _, streak = _make(tmp_path)
    _seed_activity(db, "B", DAY1, local=True, peer=False)
    out = streak.settle("B", "A", now=DAY1, connected=True)
    assert out.status == SettleStatus.OK
    assert out.streak == 0
    assert out.grace_status == "grace"
    assert out.grace_left == 3
    assert out.event_sent is False
    assert sync.calls == []
    db.close()


def test_grace_recovery_after_3_days(tmp_path) -> None:
    db, _, _, streak = _make(tmp_path)
    _preseed(db, streak=3, grace="grace", grace_left=3, grace_progress=0)
    # 连续 3 日双活跃
    for d in (DAY1, DAY2, DAY3):
        _seed_activity(db, "B", d, local=True, peer=True)
    out1 = streak.settle("B", "A", now=DAY1)
    assert (out1.streak, out1.grace_status, out1.grace_progress) == (4, "grace", 1)
    out2 = streak.settle("B", "A", now=DAY2)
    assert (out2.streak, out2.grace_status, out2.grace_progress) == (5, "grace", 2)
    out3 = streak.settle("B", "A", now=DAY3)
    assert (out3.streak, out3.grace_status) == (6, "normal")
    assert out3.grace_left == 3  # 恢复后重置缓冲
    db.close()


def test_grace_miss_decrements(tmp_path) -> None:
    db, _, _, streak = _make(tmp_path)
    _preseed(db, streak=3, grace="grace", grace_left=2, grace_progress=1)
    out = streak.settle("B", "A", now=DAY1, connected=True)  # 无活动
    assert out.grace_status == "grace"
    assert out.grace_left == 1
    assert out.grace_progress == 0
    assert out.streak == 3
    db.close()


def test_grace_exhaust_resets_streak(tmp_path) -> None:
    db, _, _, streak = _make(tmp_path)
    _preseed(db, streak=3, grace="grace", grace_left=1, grace_progress=0)
    out = streak.settle("B", "A", now=DAY1, connected=True)  # 无活动
    assert out.streak == 0
    assert out.grace_status == "normal"
    assert out.grace_left == 3
    db.close()


def test_single_generator_only(tmp_path) -> None:
    # A 端（非生成方）与 B 端（生成方）各自独立数据目录结算
    db_a, sync_a, _, streak_a = _make(tmp_path / "a")
    db_b, sync_b, _, streak_b = _make(tmp_path / "b")
    _seed_activity(db_a, "A", DAY1, local=True, peer=True)
    _seed_activity(db_b, "B", DAY1, local=True, peer=True)
    out_a = streak_a.settle("A", "B", now=DAY1)
    out_b = streak_b.settle("B", "A", now=DAY1)
    assert out_a.event_sent is False and out_a.awarded_delta == 0
    assert out_b.event_sent is True and out_b.awarded_delta == 2
    assert sync_a.calls == []
    assert len(sync_b.calls) == 1
    db_a.close()
    db_b.close()


def test_idempotent_same_day(tmp_path) -> None:
    db, sync, _, streak = _make(tmp_path)
    _seed_activity(db, "B", DAY1, local=True, peer=True)
    out1 = streak.settle("B", "A", now=DAY1)
    out2 = streak.settle("B", "A", now=DAY1 + 100)  # 同日再结算
    assert out1.streak == 1
    assert out2.streak == 1  # 不重复 +1
    assert len(sync.calls) == 1  # 不重复发 streak 事件
    db.close()


def test_deferred_when_offline_without_peer_evidence(tmp_path) -> None:
    db, sync, _, streak = _make(tmp_path)
    _seed_activity(db, "B", DAY1, local=True, peer=False)  # 无对方证据
    out = streak.settle("B", "A", now=DAY1, connected=False)
    assert out.status == SettleStatus.DEFERRED
    assert out.streak == 0
    assert sync.calls == []
    # 恢复连接后补结算 → 转 grace
    out2 = streak.settle("B", "A", now=DAY1 + 100, connected=True)
    assert out2.status == SettleStatus.OK
    assert out2.grace_status == "grace"
    db.close()


def test_anniversary_streak_doubled(tmp_path) -> None:
    db, sync, _, streak = _make(tmp_path)
    _seed_activity(db, "B", DAY1, local=True, peer=True)
    set_anniversary_hook(lambda: True)
    try:
        out = streak.settle("B", "A", now=DAY1)
        assert out.awarded_delta == 4  # (1×2) ×2 纪念日加成
        assert sync.calls[0][1]["delta"] == 4
    finally:
        set_anniversary_hook(lambda: False)
    db.close()


def test_align_handshake_streak_max_fix(tmp_path) -> None:
    db, _, _, streak = _make(tmp_path)
    _seed_activity(db, "B", DAY1, local=True, peer=True)
    streak.settle("B", "A", now=DAY1)
    # 对方快照 streak=3 > 本端 1 → 只增修复（复用 daily_sync）
    res = handle_peer_snapshot(db, {"streak": 3, "intimacy": 0, "ts": DAY1})
    assert res.value == "repaired"
    row = db.query_one("SELECT value FROM pet_state WHERE key='streak_days'")
    assert int(row["value"]) == 3
    db.close()
