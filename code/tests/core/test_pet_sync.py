"""积分事件接收合入测试（pet-growth impl §4.2 / plan S2）。"""

from __future__ import annotations

import pytest

from core.consistency import sign_intimacy_event
from core.db import init_core
from core.pet_sync import PetSync
from sync.events import Message

MAC_KEY = b"k" * 32
OTHER_KEY = b"x" * 32
TS = 1786000000


def _msg(payload: dict, from_id: str = "peer-a") -> Message:
    return Message(v=1, type="pet.feed", from_id=from_id, seq=1, ts=TS, payload=payload)


def _ev(delta: int, reason: str, ts: int, *, sig: str | None = None) -> dict:
    return {
        "delta": delta,
        "reason": reason,
        "ts": ts,
        "sig": sig if sig is not None else sign_intimacy_event(MAC_KEY, delta, reason, ts),
    }


def _intimacy(db) -> int:
    row = db.query_one("SELECT value FROM pet_state WHERE key='intimacy'")
    return int(row["value"]) if row else 0


def test_valid_event_applies(tmp_path) -> None:
    db = init_core(tmp_path)
    seen: list[int] = []
    sync = PetSync(db, object(), MAC_KEY, on_intimacy=seen.append)
    assert sync.handle_pet_feed(_msg(_ev(3, "feed", TS))) is True
    assert _intimacy(db) == 3
    assert seen == [3]
    db.close()


def test_forged_signature_rejected(tmp_path) -> None:
    db = init_core(tmp_path)
    seen: list[int] = []
    sync = PetSync(db, object(), MAC_KEY, on_intimacy=seen.append)
    ev = _ev(3, "feed", TS)
    ev["delta"] = 99  # 篡改 delta，sig 不变 → 验签失败
    assert sync.handle_pet_feed(_msg(ev)) is False
    assert _intimacy(db) == 0
    assert seen == []
    db.close()


def test_wrong_key_rejected(tmp_path) -> None:
    db = init_core(tmp_path)
    sync = PetSync(db, object(), MAC_KEY)
    # 用 OTHER_KEY 签名的负载，本端 MAC_KEY 校验 → 拒绝
    bad = _ev(3, "feed", TS, sig=sign_intimacy_event(OTHER_KEY, 3, "feed", TS))
    assert sync.handle_pet_feed(_msg(bad)) is False
    assert _intimacy(db) == 0
    db.close()


def test_daily_limit_rejected(tmp_path) -> None:
    db = init_core(tmp_path)
    sync = PetSync(db, object(), MAC_KEY)
    assert sync.handle_pet_feed(_msg(_ev(5, "chat", TS))) is True
    assert sync.handle_pet_feed(_msg(_ev(5, "chat", TS + 3600))) is False  # 当日第 2 次
    assert _intimacy(db) == 5
    db.close()


def test_malformed_payload_rejected(tmp_path) -> None:
    db = init_core(tmp_path)
    sync = PetSync(db, object(), MAC_KEY)
    assert sync.handle_pet_feed(_msg({"delta": "x", "reason": "feed"})) is False
    assert sync.handle_pet_feed(_msg({})) is False
    assert _intimacy(db) == 0
    db.close()


def test_halt_rejects(tmp_path) -> None:
    db = init_core(tmp_path)
    sync = PetSync(db, object(), MAC_KEY)
    db.execute(
        "INSERT INTO kv(key, value) VALUES('intimacy_halted', '1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )
    assert sync.handle_pet_feed(_msg(_ev(3, "feed", TS))) is False
    assert _intimacy(db) == 0
    db.close()


def test_set_mac_key_switch(tmp_path) -> None:
    db = init_core(tmp_path)
    sync = PetSync(db, object(), MAC_KEY)
    # 旧密钥事件拒绝
    assert sync.handle_pet_feed(_msg(_ev(3, "feed", TS))) is True
    # 切换密钥后：旧密钥事件拒绝、新密钥事件通过
    sync.set_mac_key(OTHER_KEY)
    old_sig = sign_intimacy_event(MAC_KEY, 3, "feed", TS + 100)
    assert sync.handle_pet_feed(_msg(_ev(3, "feed", TS + 100, sig=old_sig))) is False
    new_sig = sign_intimacy_event(OTHER_KEY, 3, "feed", TS + 200)
    assert sync.handle_pet_feed(_msg(_ev(3, "feed", TS + 200, sig=new_sig))) is True
    assert _intimacy(db) == 6
    db.close()
