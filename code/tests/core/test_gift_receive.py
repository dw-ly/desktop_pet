"""接收与打开确认测试（gift-exchange S3）。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.db import init_core
from core.gift_config import (
    CUSTOM_EGG_ID,
    GiftState,
    load_gift_config,
    sign_unlock,
)
from core.gift_receive import GiftReceive
from core.gift_store import GiftStore
from sync.constants import PROTOCOL_VERSION
from sync.events import EventType, Message

REPO_MANIFEST = Path(__file__).resolve().parents[3] / "assets" / "manifest.json"
MAC = b"\xcd" * 32


class FakeSync:
    def __init__(self):
        self.sent = []

    def send(self, etype, payload, **kw):
        self.sent.append((etype, payload))


def _msg(etype: str, payload: dict, from_id="peerA", ts=1_000_000) -> Message:
    return Message(
        v=PROTOCOL_VERSION, type=etype, from_id=from_id, seq=1, ts=ts, payload=payload,
    )


@pytest.fixture
def env(tmp_path):
    db = init_core(tmp_path)
    store = GiftStore(db)
    sync = FakeSync()
    cfg = load_gift_config(manifest_path=REPO_MANIFEST)
    forged = []
    unlocked = []
    recv = GiftReceive(
        store, sync, MAC, cfg=cfg,
        on_forged=lambda gid: forged.append(gid),
        on_unlocked=lambda gid, iid: unlocked.append((gid, iid)),
    )
    yield SimpleNamespace(
        db=db, store=store, sync=sync, recv=recv, cfg=cfg,
        forged=forged, unlocked=unlocked,
    )
    db.close()


def _send_payload(gift_id="g1", item="outfit-heart-100", expire=1_000_100, egg=None):
    sig = sign_unlock(MAC, gift_id, item, expire)
    p = {
        "giftId": gift_id, "item": item, "to": "peerB",
        "expireAt": expire, "unlockSig": sig,
    }
    if egg is not None:
        p["eggText"] = egg
    return p, sig


def test_handle_send_inbox(env):
    p, _ = _send_payload()
    assert env.recv.handle_send(_msg(EventType.GIFT_SEND.value, p))
    offer = env.store.get_offer("g1")
    assert offer is not None
    assert offer.state == GiftState.SENT
    assert offer.from_peer == "peerA"


def test_accept_unlocks_and_sends_accept(env):
    p, sig = _send_payload()
    env.recv.handle_send(_msg(EventType.GIFT_SEND.value, p))
    assert env.recv.accept("g1")
    assert env.store.get_offer("g1").state == GiftState.ACCEPTED
    item = env.store.get_item("outfit-heart-100")
    assert item is not None
    assert item.source.startswith("gift:")
    assert len(env.sync.sent) == 1
    etype, payload = env.sync.sent[0]
    assert etype == EventType.GIFT_ACCEPT
    assert payload["giftId"] == "g1"
    assert payload["unlockSig"] == sig
    # intimacy +5
    row = env.db.query_one("SELECT value FROM pet_state WHERE key='intimacy'")
    assert int(row["value"]) == 5


def test_forged_sig_rejected(env):
    p, _ = _send_payload()
    env.recv.handle_send(_msg(EventType.GIFT_SEND.value, p))
    assert not env.recv.accept("g1", unlock_sig="0" * 64)
    assert env.forged == ["g1"]
    assert env.store.get_offer("g1").state == GiftState.SENT
    assert env.store.get_item("outfit-heart-100") is None
    assert env.sync.sent == []


def test_later_keeps_pending(env):
    p, _ = _send_payload()
    env.recv.handle_send(_msg(EventType.GIFT_SEND.value, p))
    assert env.recv.later("g1")
    assert env.store.get_offer("g1").state == GiftState.SENT
    assert env.sync.sent == []


def test_handle_accept_on_sender(env):
    # 发送端已有 offer + pending
    now = 1_000_000
    env.store.create_offer(
        gift_id="g1", from_peer="peerA", to_peer="peerB",
        item_id="outfit-heart-100", sent_at=now, expire_at=now + 100,
    )
    env.store.set_pending_unlock(
        item_id="outfit-heart-100", name="爱心", item_type="outfit",
        rarity="event", gift_id="g1", expire_at=None,
    )
    sig = sign_unlock(MAC, "g1", "outfit-heart-100", now + 100)
    m = _msg(
        EventType.GIFT_ACCEPT.value,
        {"giftId": "g1", "item": "outfit-heart-100", "unlockSig": sig},
        from_id="peerB",
    )
    assert env.recv.handle_accept(m)
    assert env.store.get_offer("g1").state == GiftState.ACCEPTED
    assert env.store.get_item("outfit-heart-100").source.startswith("gift:")
    row = env.db.query_one("SELECT value FROM pet_state WHERE key='intimacy'")
    assert int(row["value"]) == 5


def test_egg_accept_and_max(env):
    cfg = load_gift_config({"egg_max": 2}, manifest_path=REPO_MANIFEST)
    env.recv._cfg = cfg
    for i in range(3):
        gid = f"egg{i}"
        p, _ = _send_payload(gift_id=gid, item=CUSTOM_EGG_ID, egg=f"text-{i}")
        env.recv.handle_send(_msg(EventType.GIFT_SEND.value, p, ts=1_000_000 + i))
        assert env.recv.accept(gid, now=1_000_000 + i)
    assert env.store.egg_count() == 2
    texts = [e.name for e in env.store.list_eggs()]
    assert "text-0" not in texts
    assert set(texts) == {"text-1", "text-2"}


def test_handle_send_idempotent(env):
    p, _ = _send_payload()
    assert env.recv.handle_send(_msg(EventType.GIFT_SEND.value, p))
    assert env.recv.handle_send(_msg(EventType.GIFT_SEND.value, p))
    assert len(env.store.list_offers()) == 1
