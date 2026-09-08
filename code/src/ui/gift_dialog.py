"""礼物 UI 纯助手（对应 gift-exchange impl §8 / plan C1）。

- 目录分组、确认文案、接收提示、状态标签、彩蛋校验提示
- 纯函数可单测；Qt 外壳占位（同 pet_panel / anniv_panel）
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Iterable

from core.gift_config import (
    CUSTOM_EGG_ID,
    GIFT_TYPES,
    GiftConfig,
    GiftItem,
    GiftState,
    load_gift_config,
    validate_egg_text,
)

log = logging.getLogger(__name__)

try:  # pragma: no cover
    from PySide6.QtWidgets import QLabel, QWidget

    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False

_TYPE_LABELS = {
    "outfit": "装扮",
    "action": "动作",
    "emoji": "表情",
    "custom_egg": "自定义彩蛋",
}

_STATE_LABELS = {
    GiftState.SENT: "已发送",
    GiftState.ACCEPTED: "已接受",
    GiftState.EXPIRED: "已过期退回",
}


def group_catalog(items: Iterable[GiftItem]) -> dict[str, list[GiftItem]]:
    """按类型分组；键顺序固定为 GIFT_TYPES。"""
    buckets: dict[str, list[GiftItem]] = {t: [] for t in GIFT_TYPES}
    for it in items:
        if it.type in buckets:
            buckets[it.type].append(it)
    return buckets


def type_label(gift_type: str) -> str:
    return _TYPE_LABELS.get(gift_type, gift_type)


def confirm_copy(item: GiftItem, *, egg_text: str | None = None) -> str:
    """二次确认弹窗文案。"""
    if item.type == "custom_egg" or item.id == CUSTOM_EGG_ID:
        preview = (egg_text or "").strip()
        if len(preview) > 40:
            preview = preview[:40] + "…"
        return f"确认送给 TA 这份自定义彩蛋？\n「{preview}」\n发送后不可撤回。"
    return f"确认送给 TA「{item.name}」？\n这是一份郑重的礼物，发送后不可撤回。"


def receive_prompt(item_name: str | None = None) -> str:
    """接收弹窗提示。"""
    if item_name:
        return f"TA 送了你一份礼物 🎁\n「{item_name}」"
    return "TA 送了你一份礼物 🎁"


def status_label(state: GiftState | str) -> str:
    if isinstance(state, str):
        try:
            state = GiftState(state)
        except ValueError:
            return state
    return _STATE_LABELS.get(state, str(state))


def unlock_success_text(item_name: str) -> str:
    return f"已解锁「{item_name}」🎉"


def egg_validation_hint(text: str | None, cfg: GiftConfig | None = None) -> str | None:
    """彩蛋输入提示：None=合法；否则返回错误文案。"""
    cfg = cfg or load_gift_config()
    if text is None or not text.strip():
        return "彩蛋内容不能为空"
    if len(text.strip()) > cfg.egg_max_len:
        return f"彩蛋最多 {cfg.egg_max_len} 字（当前 {len(text.strip())}）"
    if validate_egg_text(text, cfg) is None:
        return "彩蛋内容无效"
    return None


def resend_hint(state: GiftState | str) -> str | None:
    """过期后退回时可重新发送。"""
    if status_label(state) == "已过期退回":
        return "礼物已过期退回，可以重新发送"
    return None


if HAS_QT:  # pragma: no cover

    class GiftDialog(QWidget):
        """礼物选择 / 确认 / 接收弹窗占位（随主项目外壳接入）。"""

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self._label = QLabel("—", self)
            self._label.setMinimumWidth(200)

        def show_receive(self, item_name: str | None = None) -> None:
            self._label.setText(receive_prompt(item_name))
            log.info("%s", receive_prompt(item_name))

        def show_unlock(self, item_name: str) -> None:
            self._label.setText(unlock_success_text(item_name))
