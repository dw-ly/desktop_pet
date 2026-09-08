"""成长面板摘要文本测试（pet-growth impl §8.1 / plan C1）。"""

from __future__ import annotations

from ui.pet_panel import pet_status_text


def test_normal_summary():
    assert pet_status_text(5, 3, "normal") == "5 级 · 连续 3 天"


def test_grace_tag():
    assert pet_status_text(5, 3, "grace") == "5 级 · 连续 3 天 · 灰色期"


def test_zero_level():
    assert pet_status_text(0, 0, "normal") == "0 级 · 连续 0 天"
