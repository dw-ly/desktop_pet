"""接收 mood.sync → 更新对方情绪内存态 + 表现覆盖 + UI 回调（对应 mood-sync impl §5 / plan S3）。

- 校验规则（impl §1.1）：负载含 `privacy == True` → 隐私通知（跳过情绪字段校验）；
  否则三字段均须合法（valence ∈ [-1.0, 1.0]、arousal ∈ [0, 1.0]、label ∈ MOOD_LABELS），任一非法 → 丢弃。
- privacy 通知语义（决策 D19）：置对方隐身标记，**不更新 partner_mood**（保留最近标签，
  表现稳定不闪烁）；下次正常情绪帧到达时 `partner_stealth` 复位为 False。
- 表现映射仅查 `MOOD_ANIMATION`（G1），本类不依赖主项目动画状态机（决策 D20）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from core.mood_config import MOOD_ANIMATION
from core.mood_export import Emotion, valid_mood_values


@dataclass(frozen=True)
class MoodEvent:
    """UI 回调负载：对方情绪 + 隐身状态。"""

    emotion: Emotion | None  # 对方当前情绪（最近标签）；privacy 通知时不更新
    stealth: bool            # 对方是否隐身


class MoodReceiver:
    """接收 mood.sync → 更新对方情绪内存态 + 表现覆盖 + UI 回调。"""

    def __init__(self, on_mood_change: Callable[[MoodEvent], None] | None = None) -> None:
        # on_mood_change: UI 回调（Qt 信号队列 marshal 由 UI 侧负责）
        self._on_mood_change = on_mood_change
        self._partner_mood: Emotion | None = None
        self._partner_stealth = False

    def handle(self, payload: dict) -> bool:
        """处理一条 mood.sync（§1.1 校验规则）。非法 → 返回 False。
        - privacy 通知：partner_stealth=True，不更新 partner_mood（保留最近标签，D19）
        - 情绪帧：partner_mood=emotion，partner_stealth=False
        两者均触发 on_mood_change(MoodEvent(...))。"""
        if not isinstance(payload, dict):
            return False
        if payload.get("privacy") is True:
            self._partner_stealth = True
            self._notify(MoodEvent(self._partner_mood, True))
            return True
        v, a, label = payload.get("valence"), payload.get("arousal"), payload.get("label")
        if not valid_mood_values(v, a, label):
            return False
        self._partner_mood = Emotion(float(v), float(a), label)
        self._partner_stealth = False
        self._notify(MoodEvent(self._partner_mood, False))
        return True

    def partner_mood(self) -> Emotion | None:
        """对方当前情绪（最近标签）。"""
        return self._partner_mood

    def partner_stealth(self) -> bool:
        """对方是否隐身。"""
        return self._partner_stealth

    def clear_partner_state(self) -> None:
        """清空对方状态（partner_mood=None、partner_stealth=False）并触发回调——
        本端开启隐身时调用（spec §3.3.6：清除本端已展示的对方状态）。"""
        self._partner_mood = None
        self._partner_stealth = False
        self._notify(MoodEvent(None, False))

    def animation_override(self, local_active: bool = False) -> str | None:
        """返回应覆盖本端团子空闲态的抽象动画标识：
        - 对方隐身 → 保留最近标签映射（MOOD_ANIMATION[last label]），表现稳定不闪烁（D19）
        - 无标签 / 对方无情绪 → None
        - local_active=True（本地交互进行中）→ None（本地交互优先，spec §3.3.4）"""
        if local_active:
            return None
        if self._partner_mood is None:
            return None
        return MOOD_ANIMATION[self._partner_mood.label]

    def _notify(self, evt: MoodEvent) -> None:
        if self._on_mood_change is not None:
            self._on_mood_change(evt)
