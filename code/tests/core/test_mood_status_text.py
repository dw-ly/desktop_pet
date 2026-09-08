"""情绪子区域展示文本测试（mood-sync impl §7.1）。"""

from __future__ import annotations

from core.mood_export import Emotion
from ui.partner_state import MOOD_SOFT_TEXT, mood_status_text


def test_soft_text_all_labels():
    for label, text in MOOD_SOFT_TEXT.items():
        assert mood_status_text(Emotion(0.5, 0.5, label), False) == text


def test_stealth_priority():
    assert mood_status_text(Emotion(0.8, 0.6, "happy"), True) == "TA 开启了隐身"


def test_empty_placeholder():
    assert mood_status_text(None, False) == "—"


def test_no_soft_text_fallback():
    # 未知标签不应出现在软化文案表外；防御性回退到占位
    assert mood_status_text(Emotion(0.5, 0.5, "unknown"), False) == "—"
