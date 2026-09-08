"""成长配置与解锁映射测试（pet-growth impl §2.4 / plan G1）。"""

from __future__ import annotations

import pytest

from core.pet_config import (
    ACTIVITY_TYPES,
    UNLOCK_ITEMS,
    PetConfig,
    earned_unlock_ids,
    level_from_intimacy,
    load_pet_config,
)


def test_default_config() -> None:
    cfg = load_pet_config()
    assert cfg.feed_delta == 3
    assert cfg.carry_delta == 10
    assert cfg.chat_delta == 5
    assert cfg.gift_delta == 5
    assert cfg.gift_anniversary_delta == 20
    assert cfg.carry_min_interval == 300.0
    assert cfg.level_k == 100
    assert cfg.level_milestones == (5, 10, 15)
    assert cfg.intimacy_milestones == (100, 365, 1000)
    assert cfg.grace_days == 3
    assert cfg.grace_recover_days == 3


def test_partial_overrides() -> None:
    cfg = load_pet_config({"feed_delta": 4})
    assert cfg.feed_delta == 4
    assert cfg.carry_delta == 10  # 其余默认


def test_milestone_list_coerced_to_tuple() -> None:
    cfg = load_pet_config({"level_milestones": [5, 10, 15]})
    assert isinstance(cfg.level_milestones, tuple)


def test_invalid_delta_raises() -> None:
    with pytest.raises(ValueError):
        load_pet_config({"feed_delta": 0})
    with pytest.raises(ValueError):
        load_pet_config({"carry_delta": -1})


def test_invalid_milestone_raises() -> None:
    with pytest.raises(ValueError):
        load_pet_config({"level_milestones": (10, 5)})
    with pytest.raises(ValueError):
        load_pet_config({"intimacy_milestones": ()})
    with pytest.raises(ValueError):
        load_pet_config({"grace_recover_days": 0})


def test_level_curve_boundaries() -> None:
    assert level_from_intimacy(0) == 0
    assert level_from_intimacy(99) == 0
    assert level_from_intimacy(100) == 1
    assert level_from_intimacy(400) == 2
    assert level_from_intimacy(2500) == 5
    assert level_from_intimacy(10000) == 10


def test_level_curve_custom_k() -> None:
    assert level_from_intimacy(2500, k=400) == 2


def test_earned_unlock_partial() -> None:
    cfg = load_pet_config()
    earned = earned_unlock_ids(cfg, 5, 100)
    assert "unlock_level_5" in earned
    assert "unlock_intimacy_100" in earned
    assert "unlock_level_10" not in earned
    assert "unlock_level_15" not in earned
    assert "unlock_intimacy_365" not in earned
    assert "unlock_intimacy_1000" not in earned


def test_earned_unlock_all() -> None:
    cfg = load_pet_config()
    earned = earned_unlock_ids(cfg, 15, 1000)
    assert earned == set(UNLOCK_ITEMS)


def test_activity_types() -> None:
    assert "pet.feed" in ACTIVITY_TYPES
    assert "mood.sync" in ACTIVITY_TYPES
    assert "date.sync" not in ACTIVITY_TYPES  # 对齐自触发，排除（D24）
