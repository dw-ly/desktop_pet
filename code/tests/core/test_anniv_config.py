"""纪念日配置测试（anniversary impl §2.3 / plan G1）。"""

from __future__ import annotations

import pytest

from core.anniv_config import (
    BLESSING_STYLES,
    BLESSING_TEMPLATES,
    DEFAULT_PRESETS,
    AnnivConfig,
    load_anniv_config,
)


def test_default_config():
    cfg = load_anniv_config()
    assert isinstance(cfg, AnnivConfig)
    assert cfg.outfit_item_id == "anniv_limited_suit"
    assert cfg.outfit_duration_days == 1
    assert cfg.blessing_timeout == 3.0
    assert cfg.default_blessing_style == "cute"
    assert cfg.default_calendar == "solar"
    assert cfg.default_repeat == "yearly"
    assert cfg.default_notify_days == 1


def test_partial_override():
    cfg = load_anniv_config({"outfit_duration_days": 3})
    assert cfg.outfit_duration_days == 3
    assert cfg.default_notify_days == 1  # 其余默认


def test_invalid_duration():
    with pytest.raises(ValueError):
        load_anniv_config({"outfit_duration_days": 0})
    with pytest.raises(ValueError):
        load_anniv_config({"blessing_timeout": 0})


def test_invalid_style():
    with pytest.raises(ValueError):
        load_anniv_config({"default_blessing_style": "x"})


def test_invalid_defaults():
    with pytest.raises(ValueError):
        load_anniv_config({"default_notify_days": -1})
    with pytest.raises(ValueError):
        load_anniv_config({"default_calendar": "hebrew"})
    with pytest.raises(ValueError):
        load_anniv_config({"default_repeat": "weekly"})


def test_presets():
    assert len(DEFAULT_PRESETS) >= 4
    titles = [p["title"] for p in DEFAULT_PRESETS]
    assert "在一起纪念日" in titles
    qixi = next(p for p in DEFAULT_PRESETS if p["title"] == "七夕")
    assert qixi["calendar"] == "lunar"
    assert qixi["repeat"] == "yearly"


def test_templates():
    assert set(BLESSING_STYLES) == {"cute", "deep"}
    for style in BLESSING_STYLES:
        texts = BLESSING_TEMPLATES[style]
        assert texts, f"{style} 模板非空"
        for t in texts:
            assert "{title}" in t
