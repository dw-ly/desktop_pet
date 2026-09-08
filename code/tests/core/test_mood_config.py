"""情绪同步公共基础测试（mood-sync impl §2.4）。"""

from __future__ import annotations

import pytest

from core.mood_config import MOOD_ANIMATION, MOOD_LABELS, MoodConfig, load_mood_config


def test_label_set_complete():
    # 8 项，与主项目 07 EMOTION_MAP 键集一致（spec §3.1）
    assert len(MOOD_LABELS) == 8
    assert set(MOOD_LABELS) == {
        "happy", "excited", "calm", "neutral",
        "sad", "angry", "confused", "sleepy",
    }


def test_animation_mapping_cover_all():
    # 无缺失 / 无多余
    assert set(MOOD_ANIMATION) == set(MOOD_LABELS)
    # 每个标签都有抽象表现标识
    for label in MOOD_LABELS:
        assert MOOD_ANIMATION[label].startswith("idle_")


def test_default_config():
    cfg = load_mood_config()
    assert isinstance(cfg, MoodConfig)
    assert cfg.threshold == 0.3
    assert cfg.min_interval == 600.0
    assert cfg.sample_period == 300.0
    assert cfg.stealth_default is False


def test_partial_override():
    cfg = load_mood_config({"threshold": 0.5})
    assert cfg.threshold == 0.5
    assert cfg.min_interval == 600.0  # 其余保持默认
    assert cfg.sample_period == 300.0
    assert cfg.stealth_default is False


def test_all_fields_override():
    cfg = load_mood_config({
        "threshold": 0.1, "min_interval": 60.0,
        "sample_period": 30.0, "stealth_default": True,
    })
    assert cfg.threshold == 0.1
    assert cfg.min_interval == 60.0
    assert cfg.sample_period == 30.0
    assert cfg.stealth_default is True


def test_invalid_threshold_raises():
    for bad in (2.0, -0.1, float("inf"), float("nan"), "x"):
        with pytest.raises(ValueError):
            load_mood_config({"threshold": bad})


def test_invalid_min_interval_raises():
    for bad in (0, -1, float("nan")):
        with pytest.raises(ValueError):
            load_mood_config({"min_interval": bad})


def test_invalid_sample_period_raises():
    for bad in (0, -300.0):
        with pytest.raises(ValueError):
            load_mood_config({"sample_period": bad})


def test_invalid_stealth_default_raises():
    with pytest.raises(ValueError):
        load_mood_config({"stealth_default": "yes"})


def test_unknown_override_ignored():
    cfg = load_mood_config({"unknown": 1, "threshold": 0.7})
    assert cfg.threshold == 0.7
    assert cfg.min_interval == 600.0
