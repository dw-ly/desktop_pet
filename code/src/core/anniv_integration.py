"""养成联动钩子（anniversary impl §7 / plan S5）。

注册 `is_anniversary_today()` 到 pet-growth 钩子（D27 注入点）：
pet-growth 积分计算自动对当天基础 reason ×2（礼物 +20 特惠不叠加由 pet-growth 侧结算）。
"""

from __future__ import annotations

from datetime import date

from .pet_growth import set_anniversary_hook


def register_anniversary_hook(calendar) -> None:
    """注册钩子：is_anniversary_today() = calendar.is_anniversary(date.today())。
    重复注册以最后一次为准。"""
    set_anniversary_hook(lambda: calendar.is_anniversary(date.today()))


def unregister_anniversary_hook() -> None:
    """恢复缺省（恒 False）。测试隔离 / demo 收尾用。"""
    set_anniversary_hook(lambda: False)
