"""TA 状态条 · 情绪标签子区域（对应 mood-sync impl §7.1 / plan C1）。

与 sync-security C1 共用 `partner_state.py` 文件：连接/配对区由 sync-security 负责，
本模块负责**情绪标签子区域**（最近情绪软化文案 + 隐身提示），职责不重叠。

展示逻辑：
- partner_mood 非空 → 显示 `MOOD_SOFT_TEXT[label]`（只显示标签软化文案，不显示原文）
- partner_stealth → 叠加/替换为 "TA 开启了隐身"（保留最近标签表现，D19）
- 两者皆空 → 占位 "—"

PySide6 条件导入 + HAS_QT 占位（同 `settings_dialog.py` 模式）；无 Qt 环境时
`mood_status_text` 纯函数仍可单测（本仓库不依赖 PySide6）。
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

try:  # pragma: no cover - 依赖主项目外壳提供的 PySide6
    from PySide6.QtCore import Signal
    from PySide6.QtWidgets import QLabel, QWidget

    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False

from core.mood_receive import MoodEvent

# 软化文案映射（只显示标签软化文案，不显示原文）
MOOD_SOFT_TEXT: dict[str, str] = {
    "happy":    "TA 心情不错",
    "excited":  "TA 有点兴奋",
    "calm":     "TA 很平静",
    "neutral":  "TA 状态一般",
    "sad":      "TA 有点难过",
    "angry":    "TA 好像生气了",
    "confused": "TA 有点困惑",
    "sleepy":   "TA 今天有点累",
}

_STEALTH_TEXT = "TA 开启了隐身"
_PLACEHOLDER = "—"


def mood_status_text(emotion, stealth: bool) -> str:
    """情绪子区域展示文本（纯函数，可单测）。

    - 隐身 → "TA 开启了隐身"（优先展示）
    - 有最近情绪标签 → 软化文案
    - 皆空 → 占位 "—"
    """
    if stealth:
        return _STEALTH_TEXT
    if emotion is not None and emotion.label in MOOD_SOFT_TEXT:
        return MOOD_SOFT_TEXT[emotion.label]
    return _PLACEHOLDER


if HAS_QT:  # pragma: no cover - 依赖 PySide6

    class MoodStateBar(QWidget):
        """情绪标签子区域：订阅 MoodReceiver.on_mood_change 更新文本。

        回调在 sync 线程触发 → 经 Qt 信号队列 marshal 回 UI 线程（不阻塞主线程，
        plan C1 验收）。主项目外壳接入时实例化并 `mgr.add_handler(EventType.MOOD_SYNC,
        receiver.handle)` + `receiver = MoodReceiver(on_mood_change=bar.on_mood_change)`。
        """

        changed = Signal(object)  # MoodEvent

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self._label = QLabel(_PLACEHOLDER, self)
            self._label.setMinimumWidth(120)
            self.changed.connect(self._apply_event)

        def on_mood_change(self, evt: MoodEvent) -> None:
            """UI 回调（任意线程调用安全：信号队列 marshal 到 UI 线程）。"""
            self.changed.emit(evt)

        def _apply_event(self, evt: MoodEvent) -> None:
            self._label.setText(mood_status_text(evt.emotion, evt.stealth))
