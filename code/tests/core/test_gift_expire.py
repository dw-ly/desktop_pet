"""过期退回与解锁失效测试（gift-exchange S4）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.db import init_core
from core.gift_config import GiftState
from core.gift_expire import GiftExpire
from core.gift_store import GiftStore
from sync.constants import PROTOCOL_VERSION
from sync.events import EventType, Message


class FakeSync:
    def __init__(self):
        self.sent = []

    def send(self, etype, payload, **kw):
        self.sent.append((etype, payload))


@pytest.fixture
def env(tmp_path):
    db = init_core(tmp_path)
    store = GiftStore(db)
    sync = FakeSync()
    expired_offers = []
    expired_items = []
    exp = GiftExpire(
        store, sync,
        on_offer_expired=lambda o: expired_offers.append(o.gift_id),
        on_items_expired=lambda ids: expired_items.extend(ids),
    )
    yield SimpleNamespace(
        db=db, store=store, sync=sync, exp=exp,
        expired_offers=expired_offers, expired_items=expired_items,
    )
    db.close()


def test_scan_offers_boundary(env):
    now = 1_000_000
    env.store.create_offer(
        gift_id="exact", from_peer="A", to_peer="B", item_id="x",
        sent_at=now - 10, expire_at=now,  # 边界：expire_at == now 不触发
    )
    env.store.create_offer(
        gift_id="past", from_peer="A", to_peer="B", item_id="y",
        sent_at=now - 20, expire_at=now - 1,
    )
    env.store.set_pending_unlock(
        item_id="y", name="y", item_type="outfit", rarity="c", gift_id="past",
    )
    ids = env.exp.scan_offers(now)
    assert ids == ["past"]
    assert env.store.get_offer("past").state == GiftState.EXPIRED
    assert env.store.get_offer("exact").state == GiftState.SENT
    assert env.store.get_item("y") is None  # pending 回退
    assert env.sync.sent[0][0] == EventType.GIFT_EXPIRE
    assert env.sync.sent[0][1] == {"giftId": "past"}


def test_handle_expire_remote(env):
    now = 1_000_000
    env.store.create_offer(
        gift_id="g1", from_peer="A", to_peer="B", item_id="x",
        sent_at=now, expire_at=now + 100,
    )
    m = Message(
        v=PROTOCOL_VERSION, type=EventType.GIFT_EXPIRE.value,
        from_id="A", seq=1, ts=now + 200, payload={"giftId": "g1"},
    )
    assert env.exp.handle_expire(m)
    assert env.store.get_offer("g1").state == GiftState.EXPIRED
    # 幂等
    assert env.exp.handle_expire(m)


def test_expire_items_tick(env):
    now = 1_000_000
    env.store.unlock_item(
        item_id="old", name="old", item_type="outfit", rarity="c",
        source="gift:A", expire_at=now,
    )
    env.store.unlock_item(
        item_id="keep", name="keep", item_type="outfit", rarity="c",
        source="gift:A", expire_at=now + 1,
    )
    result = env.exp.tick(now)
    assert result["items"] == ["old"]
    assert env.expired_items == ["old"]
    assert env.store.get_item("keep") is not None


def test_no_expire_accepted(env):
    now = 1_000_000
    env.store.create_offer(
        gift_id="g1", from_peer="A", to_peer="B", item_id="x",
        sent_at=now - 100, expire_at=now - 1,
    )
    env.store.transition("g1", GiftState.ACCEPTED)
    assert env.exp.scan_offers(now) == []
