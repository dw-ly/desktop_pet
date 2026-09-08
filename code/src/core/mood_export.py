"""本端情绪汇总与当前值维护（对应 mood-sync impl §3 / plan S1）。

- `Emotion`：主项目 07 情绪系统输出帧（valence / arousal / label）
- `MoodExporter`：**内存态**当前情绪，新值覆盖旧值；不写库、无 db 依赖（spec 行为 8）
- 数值校验与接收侧一致（valence ∈ [-1,1]、arousal ∈ [0,1]），保证本端不会对外发出非法负载

采样周期由主项目情绪系统侧控制（每 sample_period 输出一帧汇总）；本类被动接收，不自行计时。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from core.mood_config import MOOD_LABELS, MoodConfig


@dataclass(frozen=True)
class Emotion:
    """本端情绪系统输出帧（复用主项目 07）。"""

    valence: float   # -1.0 ~ +1.0
    arousal: float   # 0 ~ 1.0
    label: str       # MOOD_LABELS 之一


def valid_mood_values(valence, arousal, label) -> bool:
    """三值合法性校验（与接收侧 §1.1 一致）。

    label ∈ MOOD_LABELS；valence ∈ [-1.0, 1.0] 有限数；arousal ∈ [0, 1.0] 有限数。
    """
    if label not in MOOD_LABELS:
        return False
    try:
        v = float(valence)
        a = float(arousal)
    except (TypeError, ValueError):
        return False
    return (
        math.isfinite(v) and -1.0 <= v <= 1.0
        and math.isfinite(a) and 0.0 <= a <= 1.0
    )


class MoodExporter:
    """本端当前情绪（内存态，不持久化）。被动接收情绪系统输出帧，新值覆盖旧值。"""

    def __init__(self, config: MoodConfig | None = None) -> None:
        self._config = config or MoodConfig()
        self._current: Emotion | None = None

    def update(self, emotion: Emotion) -> bool:
        """接收一帧输出。label 非法或数值越界 → 返回 False，current 不变；
        合法 → 覆盖 current，返回 True。"""
        if not valid_mood_values(emotion.valence, emotion.arousal, emotion.label):
            return False
        self._current = emotion
        return True

    def current(self) -> Emotion | None:
        """当前情绪；未接收过合法帧返回 None。"""
        return self._current
