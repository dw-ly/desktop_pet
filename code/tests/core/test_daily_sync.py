"""每日兜底对齐测试（data-consistency impl §4.4 / plan S5）。"""

from __future__ import annotations

import json

from sync.events import EventType
from sync.transport import ConnState

from core.consistency import apply_intimacy_event, sign_intimacy_event
from core.daily_sync import (
    AlignResult,
    build_snapshot,
    daily_align,
    handle_peer_snapshot,
    replay_intimacy_total,
)
from core.db import init_core

MAC_KEY = b"k" * 32


class FakeSync:
    """SyncManager 鸭子类型（仅 connection_status/send）。"""

    def __init__(self, connected: bool = True) -> None:
        self._connected = connected
        self.sent: list[tuple] = []

    def connection_status(self) -> ConnState:
        return ConnState.CONNECTED if self._connected else ConnState.DISCONNECTED

    def send(self, type, payload, *, expires_at=None) -> None:
        self.sent.append((type, payload))


def _insert_feed(db, event_id: str, delta: int, reason: str, ts: int,
                 *, sig: str | None = None) -> None:
    payload = json.dumps({
        "delta": delta, "reason": reason, "ts": ts,
        "sig": sig or sign_intimacy_event(MAC_KEY, delta, reason, ts),
    })
    db.execute(
        "INSERT OR IGNORE INTO events(event_id, seq, type, peer, payload_json, "
        "created_at, acked_at, status, expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (event_id, None, EventType.PET_FEED.value, "B", payload, ts, None, "received", None),
    )


def _intimacy(db) -> int:
    row = db.query_one("SELECT value FROM pet_state WHERE key='intimacy'")
    return int(row["value"]) if row else 0


def _streak(db) -> int:
    row = db.query_one("SELECT value FROM pet_state WHERE key='streak_days'")
    return int(row["value"]) if row else 0


# --------------------------------------------------------------------------- #
# daily_align
# --------------------------------------------------------------------------- #

def test_align_deferred_when_disconnected(tmp_path) -> None:
    db = init_core(tmp_path)
    sync = FakeSync(connected=False)
    assert daily_align(sync, db, MAC_KEY) is AlignResult.DEFERRED
    assert sync.sent == []
    db.close()


def test_align_ok_and_sends_snapshot(tmp_path) -> None:
    db = init_core(tmp_path)
    sync = FakeSync(connected=True)
    assert daily_align(sync, db, MAC_KEY) is AlignResult.OK
    assert len(sync.sent) == 1
    etype, payload = sync.sent[0]
    assert etype is EventType.DATE_SYNC
    assert payload["streak"] == 0
    assert payload["intimacy"] == 0
    assert isinstance(payload["ts"], int)
    db.close()


def test_align_snapshot_reflects_state(tmp_path) -> None:
    db = init_core(tmp_path)
    db.execute("INSERT INTO pet_state(key, value) VALUES('streak_days', 5), ('intimacy', 42)")
    snap = build_snapshot(db)
    assert snap == {"streak": 5, "intimacy": 42, "ts": snap["ts"]}
    db.close()


def test_align_repairs_intimacy_mismatch(tmp_path) -> None:
    """事件流重放总和 != pet_state.intimacy → 修复为重放总和。"""
    db = init_core(tmp_path)
    ts = 1783000000
    _insert_feed(db, "B:1", 10, "carry", ts)
    _insert_feed(db, "B:2", 3, "feed", ts + 60)
    _insert_feed(db, "B:3", 3, "feed", ts + 120)
    db.execute("INSERT INTO pet_state(key, value) VALUES('intimacy', 999)")  # 本地偏差
    sync = FakeSync(connected=True)
    assert daily_align(sync, db, MAC_KEY) is AlignResult.REPAIRED
    assert _intimacy(db) == 16
    assert sync.sent[0][0] is EventType.DATE_SYNC
    db.close()


def test_align_no_mac_key_skips_replay(tmp_path) -> None:
    """mac_key 为空：发快照但跳过亲密度重放核对。"""
    db = init_core(tmp_path)
    _insert_feed(db, "B:1", 10, "carry", 1783000000)
    db.execute("INSERT INTO pet_state(key, value) VALUES('intimacy', 0)")
    sync = FakeSync(connected=True)
    assert daily_align(sync, db) is AlignResult.OK
    assert _intimacy(db) == 0  # 未修复
    assert len(sync.sent) == 1
    db.close()


def test_align_manual_needed_when_halted(tmp_path) -> None:
    """S4 熔断（签名连续失败）→ 不自动修复，提示手动同步。"""
    db = init_core(tmp_path)
    db.execute("INSERT INTO kv(key, value) VALUES('intimacy_halted', '1')")
    sync = FakeSync(connected=True)
    assert daily_align(sync, db, MAC_KEY) is AlignResult.MANUAL_NEEDED
    assert sync.sent == []
    db.close()


# --------------------------------------------------------------------------- #
# handle_peer_snapshot
# --------------------------------------------------------------------------- #

def test_handle_peer_snapshot_repairs_streak(tmp_path) -> None:
    db = init_core(tmp_path)
    db.execute("INSERT INTO pet_state(key, value) VALUES('streak_days', 2)")
    assert handle_peer_snapshot(db, {"streak": 5, "intimacy": 999, "ts": 0}) is AlignResult.REPAIRED
    assert _streak(db) == 5
    db.close()


def test_handle_peer_snapshot_no_diff_ok(tmp_path) -> None:
    db = init_core(tmp_path)
    db.execute("INSERT INTO pet_state(key, value) VALUES('streak_days', 5)")
    assert handle_peer_snapshot(db, {"streak": 5, "intimacy": 999, "ts": 0}) is AlignResult.OK
    assert handle_peer_snapshot(db, {"streak": 3, "intimacy": 999, "ts": 0}) is AlignResult.OK
    assert _streak(db) == 5  # 不降
    db.close()


def test_handle_peer_snapshot_ignores_bad_payload(tmp_path) -> None:
    db = init_core(tmp_path)
    assert handle_peer_snapshot(db, {}) is AlignResult.OK
    assert handle_peer_snapshot(db, {"streak": "x"}) is AlignResult.OK
    assert _streak(db) == 0
    db.close()


# --------------------------------------------------------------------------- #
# replay_intimacy_total（与 apply_intimacy_event 同构）
# --------------------------------------------------------------------------- #

def test_replay_total_matches_applied(tmp_path) -> None:
    """接收侧先入 events 再合入（queue.receive 顺序）；重放与之同构。"""
    db = init_core(tmp_path)
    ts = 1783000000
    for i, (delta, reason) in enumerate([(10, "carry"), (3, "feed"), (3, "feed")]):
        _insert_feed(db, f"B:{i}", delta, reason, ts + i * 60)
        assert apply_intimacy_event(
            db, {"delta": delta, "reason": reason, "ts": ts + i * 60,
                 "sig": sign_intimacy_event(MAC_KEY, delta, reason, ts + i * 60)},
            MAC_KEY,
        )
    assert _intimacy(db) == 16
    assert replay_intimacy_total(db, MAC_KEY) == 16
    db.close()


def test_replay_respects_daily_limit_and_skips_bad_sig(tmp_path) -> None:
    db = init_core(tmp_path)
    ts = 1783000000
    # chat 上限 1：两条同日 chat 事件，重放只计 1 条
    _insert_feed(db, "B:1", 5, "chat", ts)
    _insert_feed(db, "B:2", 5, "chat", ts + 60)
    # 非法签名：不计
    _insert_feed(db, "B:3", 10, "carry", ts + 120, sig="0" * 64)
    assert replay_intimacy_total(db, MAC_KEY) == 5
    db.close()


def test_replay_empty_when_no_feed(tmp_path) -> None:
    db = init_core(tmp_path)
    assert replay_intimacy_total(db, MAC_KEY) == 0
    db.close()
