"""礼物数据层测试（gift-exchange S1）。"""

from __future__ import annotations

import time

import pytest

from core.db import init_core
from core.gift_config import GiftState
from core.gift_store import GiftStore


@pytest.fixture
def store(tmp_path):
    db = init_core(tmp_path)
    yield GiftStore(db)
    db.close()


def test_create_and_get_offer(store):
    now = int(time.time())
    offer = store.create_offer(
        from_peer="A", to_peer="B", item_id="outfit-heart-100",
        sent_at=now, expire_at=now + 86400, gift_id="g1",
    )
    assert offer is not None
    assert offer.state == GiftState.SENT
    assert store.get_offer("g1").item_id == "outfit-heart-100"


def test_create_offer_idempotent(store):
    now = int(time.time())
    assert store.create_offer(
        from_peer="A", to_peer="B", item_id="x",
        sent_at=now, expire_at=now + 10, gift_id="g1",
    )
    assert store.create_offer(
        from_peer="A", to_peer="B", item_id="y",
        sent_at=now, expire_at=now + 10, gift_id="g1",
    ) is None
    assert store.get_offer("g1").item_id == "x"


def test_legal_transitions(store):
    now = int(time.time())
    store.create_offer(
        from_peer="A", to_peer="B", item_id="x",
        sent_at=now, expire_at=now + 10, gift_id="g1",
    )
    assert store.transition("g1", GiftState.ACCEPTED, accepted_at=now + 1)
    assert store.get_offer("g1").state == GiftState.ACCEPTED
    assert not store.transition("g1", GiftState.EXPIRED)
    assert not store.transition("g1", GiftState.SENT)


def test_sent_to_expired(store):
    now = int(time.time())
    store.create_offer(
        from_peer="A", to_peer="B", item_id="x",
        sent_at=now, expire_at=now + 10, gift_id="g2",
    )
    assert store.transition("g2", GiftState.EXPIRED)
    assert store.get_offer("g2").state == GiftState.EXPIRED


def test_illegal_transition_accepted_to_sent(store):
    now = int(time.time())
    store.create_offer(
        from_peer="A", to_peer="B", item_id="x",
        sent_at=now, expire_at=now + 10, gift_id="g3",
    )
    store.transition("g3", GiftState.ACCEPTED)
    assert not store.transition("g3", GiftState.SENT)


def test_pending_expired_offers(store):
    now = 1_000_000
    store.create_offer(
        from_peer="A", to_peer="B", item_id="x",
        sent_at=now - 100, expire_at=now - 1, gift_id="old",
    )
    store.create_offer(
        from_peer="A", to_peer="B", item_id="y",
        sent_at=now, expire_at=now + 100, gift_id="new",
    )
    expired = store.pending_expired_offers(now)
    assert [o.gift_id for o in expired] == ["old"]


def test_unlock_once(store):
    assert store.unlock_item(
        item_id="outfit-heart-100", name="爱心", item_type="outfit",
        rarity="event", source="gift:A", expire_at=None,
    )
    assert not store.unlock_item(
        item_id="outfit-heart-100", name="覆盖", item_type="outfit",
        rarity="event", source="gift:B", expire_at=None,
    )
    assert store.get_item("outfit-heart-100").name == "爱心"


def test_pending_finalize_and_remove(store):
    store.set_pending_unlock(
        item_id="outfit-crown", name="皇冠", item_type="outfit",
        rarity="event", gift_id="g9", expire_at=None,
    )
    assert store.get_item("outfit-crown").source == "pending:g9"
    assert store.finalize_pending("g9", "gift:A")
    assert store.get_item("outfit-crown").source == "gift:A"

    store.set_pending_unlock(
        item_id="outfit-halo", name="光环", item_type="outfit",
        rarity="event", gift_id="g10", expire_at=None,
    )
    assert store.remove_pending("g10") == 1
    assert store.get_item("outfit-halo") is None


def test_expire_items(store):
    now = 1_000_000
    store.unlock_item(
        item_id="a", name="a", item_type="outfit", rarity="common",
        source="gift:A", expire_at=now - 1,
    )
    store.unlock_item(
        item_id="b", name="b", item_type="outfit", rarity="common",
        source="gift:A", expire_at=now + 100,
    )
    store.unlock_item(
        item_id="c", name="c", item_type="action", rarity="common",
        source="gift:A", expire_at=None,
    )
    removed = store.expire_items(now)
    assert removed == ["a"]
    assert store.get_item("b") is not None
    assert store.get_item("c") is not None
    avail = {i.item_id for i in store.list_available_items(now)}
    assert avail == {"b", "c"}


def test_egg_max_replace_oldest(store):
    for i in range(5):
        store.add_egg(text=f"egg-{i}", source="gift:A", gift_id=f"g{i}", egg_max=3)
    eggs = store.list_eggs()
    assert len(eggs) == 3
    assert store.egg_count() == 3
    texts = [e.name for e in eggs]
    assert "egg-0" not in texts and "egg-1" not in texts
    assert texts == ["egg-2", "egg-3", "egg-4"]


def test_egg_text_kv(store):
    store.set_egg_text("g1", "想你了")
    assert store.get_egg_text("g1") == "想你了"
    store.clear_egg_text("g1")
    assert store.get_egg_text("g1") is None
