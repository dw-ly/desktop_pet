"""纪念日状态行文本测试（anniversary impl §8.1 / plan C1）。"""

from __future__ import annotations

from datetime import date

from ui.anniv_panel import anniv_status_text


def test_countdown():
    assert anniv_status_text("在一起纪念日", date(2026, 2, 14), 3) == "《在一起纪念日》还有 3 天"


def test_today():
    assert anniv_status_text("在一起纪念日", date(2026, 2, 14), 0) == "《在一起纪念日》就是今天 🎉"


def test_passed():
    assert anniv_status_text("在一起纪念日", None, 0) == "《在一起纪念日》今年已过"
