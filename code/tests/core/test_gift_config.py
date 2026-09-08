"""礼物库与配置测试（gift-exchange G1）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.gift_config import (
    CUSTOM_EGG_ID,
    GIFT_TYPES,
    GiftState,
    LEGAL_TRANSITIONS,
    item_expire_at,
    load_gift_catalog,
    load_gift_config,
    sign_unlock,
    validate_egg_text,
    verify_unlock,
)

REPO_MANIFEST = Path(__file__).resolve().parents[3] / "assets" / "manifest.json"


def test_manifest_loads_17_gifts():
    cfg = load_gift_config(manifest_path=REPO_MANIFEST)
    assert len(cfg.catalog) == 17
    types = {g.type for g in cfg.catalog}
    assert types <= set(GIFT_TYPES) - {"custom_egg"}
    heart = cfg.get("outfit-heart-100")
    assert heart is not None
    assert heart.name == "爱心气泡装扮"
    assert heart.type == "outfit"
    assert heart.rarity == "event"
    assert heart.expire_days == 30


def test_custom_egg_virtual_item():
    cfg = load_gift_config(manifest_path=REPO_MANIFEST)
    egg = cfg.get(CUSTOM_EGG_ID)
    assert egg is not None
    assert egg.type == "custom_egg"


def test_missing_manifest_empty_catalog(tmp_path):
    missing = tmp_path / "nope.json"
    catalog = load_gift_catalog(missing)
    assert catalog == ()
    cfg = load_gift_config(manifest_path=missing)
    assert cfg.catalog == ()


def test_corrupt_manifest_empty(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_gift_catalog(bad) == ()


def test_manifest_wrong_shape(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"version": 1}), encoding="utf-8")
    assert load_gift_catalog(p) == ()


def test_config_overrides():
    cfg = load_gift_config(
        {"offer_ttl_seconds": 60, "egg_max": 5, "egg_max_len": 10},
        manifest_path=REPO_MANIFEST,
    )
    assert cfg.offer_ttl_seconds == 60
    assert cfg.egg_max == 5
    assert cfg.egg_max_len == 10


def test_config_invalid_ttl():
    with pytest.raises(ValueError):
        load_gift_config({"offer_ttl_seconds": 0})


def test_states_and_transitions():
    assert set(GiftState) == {GiftState.SENT, GiftState.ACCEPTED, GiftState.EXPIRED}
    assert GiftState.ACCEPTED in LEGAL_TRANSITIONS[GiftState.SENT]
    assert GiftState.EXPIRED in LEGAL_TRANSITIONS[GiftState.SENT]
    assert not LEGAL_TRANSITIONS[GiftState.ACCEPTED]


def test_validate_egg_text():
    cfg = load_gift_config({"egg_max_len": 10}, manifest_path=REPO_MANIFEST)
    assert validate_egg_text("  hi  ", cfg) == "hi"
    assert validate_egg_text("", cfg) is None
    assert validate_egg_text("x" * 11, cfg) is None
    assert validate_egg_text(None, cfg) is None


def test_unlock_sign_verify():
    key = b"\x01" * 32
    sig = sign_unlock(key, "g1", "outfit-heart-100", 1000)
    assert verify_unlock(key, "g1", "outfit-heart-100", 1000, sig)
    assert not verify_unlock(key, "g1", "outfit-heart-100", 1001, sig)
    assert not verify_unlock(key, "g1", "other", 1000, sig)
    assert not verify_unlock(key, "g1", "outfit-heart-100", 1000, "deadbeef")
    assert not verify_unlock(key, "g1", "outfit-heart-100", 1000, "")


def test_item_expire_at():
    assert item_expire_at(0, 1000) is None
    assert item_expire_at(-1, 1000) is None
    assert item_expire_at(1, 1000) == 1000 + 86400
