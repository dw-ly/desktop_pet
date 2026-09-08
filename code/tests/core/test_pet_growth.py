"""积分计算与本地合入测试（pet-growth impl §3.3 / plan S1）。"""

from __future__ import annotations

import pytest

from core.consistency import verify_intimacy_signature
from core.db import init_core
from core.pet_config import load_pet_config
from core.pet_growth import PetGrowth, set_anniversary_hook
from sync.events import EventType

MAC_KEY = b"k" * 32  # derive_mac_key 产物（测试用任意 32B）
DAY_TS = 1786000000  # 一个固定的"今天"时间戳（2026-08-04 附近）


class FakeSync:
    """捕获发送的鸭子类型 sync（仅 send）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[EventType, dict]] = []

    def send(self, type_, payload: dict, **kwargs) -> None:
        self.calls.append((type_, payload))


@pytest.fixture(autouse=True)
def _reset_hook():
    """每个测试后重置纪念日钩子（D27 缺省 False）。"""
    set_anniversary_hook(lambda: False)
    yield


def _intimacy(db) -> int:
    row = db.query_one("SELECT value FROM pet_state WHERE key='intimacy'")
    return int(row["value"]) if row else 0


def _set_kv(db, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def test_feed_applies_and_sends(tmp_path) -> None:
    db = init_core(tmp_path)
    sync = FakeSync()
    growth = PetGrowth(db, sync, MAC_KEY)
    assert growth.award("feed", now=DAY_TS) == 3
    assert _intimacy(db) == 3
    assert len(sync.calls) == 1
    type_, payload = sync.calls[0]
    assert type_ == EventType.PET_FEED
    assert set(payload.keys()) == {"delta", "reason", "ts", "sig"}  # 载荷纯净
    assert payload["delta"] == 3
    assert payload["reason"] == "feed"
    assert payload["ts"] == DAY_TS
    assert verify_intimacy_signature(MAC_KEY, 3, "feed", DAY_TS, payload["sig"])
    db.close()


def test_carry_frequency_gate(tmp_path) -> None:
    db = init_core(tmp_path)
    sync = FakeSync()
    growth = PetGrowth(db, sync, MAC_KEY)
    assert growth.award("carry", now=DAY_TS) == 10
    assert growth.award("carry", now=DAY_TS + 60) == 0  # 间隔 < 300s → 拒绝
    assert growth.award("carry", now=DAY_TS + 300) == 10  # 恰好 300s → 放行
    assert len(sync.calls) == 2  # 仅两次发送
    db.close()


def test_chat_daily_limit(tmp_path) -> None:
    db = init_core(tmp_path)
    sync = FakeSync()
    growth = PetGrowth(db, sync, MAC_KEY)
    assert growth.award("chat", now=DAY_TS) == 5
    assert growth.award("chat", now=DAY_TS + 3600) == 0  # 当日第 2 次 → 拒绝
    assert _intimacy(db) == 5
    db.close()


def test_feed_daily_limit_sixth_rejected(tmp_path) -> None:
    db = init_core(tmp_path)
    sync = FakeSync()
    growth = PetGrowth(db, sync, MAC_KEY)
    for i in range(5):
        assert growth.award("feed", now=DAY_TS + i * 10) == 3
    assert growth.award("feed", now=DAY_TS + 50) == 0  # 第 6 次 → 拒绝
    assert _intimacy(db) == 15
    assert len(sync.calls) == 5
    db.close()


def test_anniversary_double(tmp_path) -> None:
    db = init_core(tmp_path)
    sync = FakeSync()
    set_anniversary_hook(lambda: True)
    growth = PetGrowth(db, sync, MAC_KEY)
    assert growth.award("feed", now=DAY_TS) == 6  # 3 × 2
    assert sync.calls[0][1]["delta"] == 6
    db.close()


def test_gift_anniversary_special_no_double(tmp_path) -> None:
    db = init_core(tmp_path)
    sync = FakeSync()
    set_anniversary_hook(lambda: True)
    growth = PetGrowth(db, sync, MAC_KEY)
    assert growth.award("gift", now=DAY_TS) == 20  # 特惠 20，不 ×2（D29）
    assert sync.calls[0][1]["delta"] == 20
    db.close()


def test_gift_normal(tmp_path) -> None:
    db = init_core(tmp_path)
    sync = FakeSync()
    growth = PetGrowth(db, sync, MAC_KEY)
    assert growth.award("gift", now=DAY_TS) == 5  # 非纪念日普通 5
    db.close()


def test_halt_blocks_send(tmp_path) -> None:
    db = init_core(tmp_path)
    sync = FakeSync()
    growth = PetGrowth(db, sync, MAC_KEY)
    _set_kv(db, "intimacy_halted", "1")
    assert growth.award("feed", now=DAY_TS) == 0
    assert sync.calls == []  # 熔断：不发送
    db.close()


def test_invalid_reason_raises(tmp_path) -> None:
    db = init_core(tmp_path)
    sync = FakeSync()
    growth = PetGrowth(db, sync, MAC_KEY)
    with pytest.raises(ValueError):
        growth.award("hack", now=DAY_TS)
    with pytest.raises(ValueError):
        growth.delta_for("streak")  # streak 需显式 delta（D30）
    db.close()


def test_delta_for_pure(tmp_path) -> None:
    db = init_core(tmp_path)
    sync = FakeSync()
    growth = PetGrowth(db, sync, MAC_KEY)
    assert growth.delta_for("feed") == 3
    set_anniversary_hook(lambda: True)
    assert growth.delta_for("feed") == 6
    assert growth.delta_for("gift") == 20
    assert growth.delta_for("streak", delta=4) == 8  # 纪念日 streak ×2
    db.close()


def test_custom_cfg_delta(tmp_path) -> None:
    db = init_core(tmp_path)
    sync = FakeSync()
    growth = PetGrowth(db, sync, MAC_KEY, load_pet_config({"feed_delta": 1}))
    assert growth.award("feed", now=DAY_TS) == 1
    db.close()
