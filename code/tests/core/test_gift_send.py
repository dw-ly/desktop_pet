"""赠送流程测试（gift-exchange S2）。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.db import init_core
from core.gift_config import CUSTOM_EGG_ID, GiftState, load_gift_config, verify_unlock
from core.gift_send import GiftSend
from core.gift_store import GiftStore
from sync.events import EventType

REPO_MANIFEST = Path(__file__).resolve().parents[3] / "assets" / "manifest.json"
MAC = b"\xab" * 32


class FakeSync:
    def __init__(self):
        self.sent: list[tuple] = []

    def send(self, etype, payload, **kw):
        self.sent.append((etype, payload))


@pytest.fixture
def env(tmp_path):
    db = init_core(tmp_path)
    store = GiftStore(db)
    sync = FakeSync()
    cfg = load_gift_config(
        {"offer_ttl_seconds": 100},
        manifest_path=REPO_MANIFEST,
    )
    sender = GiftSend(
        store, sync, MAC, cfg=cfg,
        my_peer_id=lambda: "peerA",
        partner_id=lambda: "peerB",
    )
    yield SimpleNamespace(db=db, store=store, sync=sync, sender=sender, cfg=cfg)
    db.close()


def test_propose_cancel_no_record(env):
    prop = env.sender.propose("outfit-heart-100")
    assert prop is not None
    env.sender.cancel()
    assert env.sender.pending is None
    assert env.store.list_offers() == []
    assert env.sync.sent == []


def test_confirm_sends_and_optimistic(env):
    assert env.sender.propose("outfit-heart-100")
    offer = env.sender.confirm(now=1_000_000)
    assert offer is not None
    assert offer.state == GiftState.SENT
    assert env.sender.pending is None
    assert len(env.sync.sent) == 1
    etype, payload = env.sync.sent[0]
    assert etype == EventType.GIFT_SEND
    assert set(payload) >= {"giftId", "item", "to", "expireAt", "unlockSig"}
    assert payload["item"] == "outfit-heart-100"
    assert payload["to"] == "peerB"
    assert payload["expireAt"] == 1_000_000 + 100
    assert verify_unlock(
        MAC, payload["giftId"], payload["item"], payload["expireAt"], payload["unlockSig"]
    )
    pending = env.store.get_item("outfit-heart-100")
    assert pending is not None
    assert pending.source.startswith("pending:")


def test_confirm_without_propose(env):
    assert env.sender.confirm() is None
    assert env.sync.sent == []


def test_egg_length_rejected(env):
    assert env.sender.propose(CUSTOM_EGG_ID, egg_text="") is None
    assert env.sender.propose(CUSTOM_EGG_ID, egg_text="x" * 201) is None
    assert env.sender.propose(CUSTOM_EGG_ID, egg_text="想你") is not None
    offer = env.sender.confirm(now=100)
    assert offer is not None
    _, payload = env.sync.sent[0]
    assert payload["eggText"] == "想你"
    assert env.store.get_egg_text(offer.gift_id) == "想你"
    # 彩蛋不写 pending user_items
    assert env.store.get_item(CUSTOM_EGG_ID) is None


def test_non_egg_with_text_rejected(env):
    assert env.sender.propose("outfit-heart-100", egg_text="nope") is None


def test_unknown_item(env):
    assert env.sender.propose("no-such-gift") is None


def test_unpaired_confirm_fails(env):
    sender = GiftSend(
        env.store, env.sync, MAC, cfg=env.cfg,
        my_peer_id=lambda: "A",
        partner_id=lambda: None,
    )
    sender.propose("outfit-heart-100")
    assert sender.confirm() is None
