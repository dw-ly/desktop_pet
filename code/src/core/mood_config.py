"""情绪同步公共基础（对应 mood-sync impl §2 / plan G1）。

- 8 情绪标签枚举（与主项目 07 EMOTION_MAP 键集完全一致，spec §3.1）
- 标签 → 空闲动画表现映射表（抽象标识，决策 D20：与主项目动画状态机解耦，
  由主项目外壳接入层映射到具体动画状态）
- 节流与采样配置加载（threshold / min_interval / sample_period / stealth_default）

纯内存、无外部依赖、无 db 依赖。
"""

from __future__ import annotations

import math
from dataclasses import dataclass


# --------------------------------------------------------------------------- #
# 情绪标签枚举
# --------------------------------------------------------------------------- #

# 8 标签（与主项目 07 EMOTION_MAP 完全一致，spec §3.1）
MOOD_LABELS: tuple[str, ...] = (
    "happy",
    "excited",
    "calm",
    "neutral",
    "sad",
    "angry",
    "confused",
    "sleepy",
)

# --------------------------------------------------------------------------- #
# 标签 → 空闲动画表现映射（人工设计先行，决策 D20）
# --------------------------------------------------------------------------- #

# 抽象表现标识（主项目外壳接入层映射到具体动画状态机；M2 内测后按反馈调优）
MOOD_ANIMATION: dict[str, str] = {
    "happy":    "idle_happy",     # 摇尾巴、跳跃
    "excited":  "idle_excited",   # 跳跃、欢快
    "calm":     "idle_calm",      # 正常待机
    "neutral":  "idle_neutral",   # 正常待机
    "sad":      "idle_sad",       # 低头、尾巴垂下
    "angry":    "idle_angry",     # 耳朵后贴、警惕姿态
    "confused": "idle_confused",  # 歪头、左右张望
    "sleepy":   "idle_tired",     # 耷拉耳朵、趴着、无精打采
}
assert set(MOOD_ANIMATION) == set(MOOD_LABELS), "8 项全覆盖、无多余"


# --------------------------------------------------------------------------- #
# 节流与采样配置
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MoodConfig:
    """情绪同步配置（impl §2.3）。"""

    threshold: float = 0.3        # 显著变化阈值：|Δvalence| 或 |Δarousal| ≥ 此值触发
    min_interval: float = 600.0   # 最小同步间隔（秒）：距上次发送 ≥ 此值才允许
    sample_period: float = 300.0  # 采样周期（秒）：主项目情绪系统输出频率（本模块不主动计时）
    stealth_default: bool = False  # 隐身默认关闭（spec §2.2 决策）


def _as_float(value, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须为数值: {value!r}") from exc


def load_mood_config(overrides: dict | None = None) -> MoodConfig:
    """配置加载：缺失键用默认值；overrides 仅覆盖给定键。
    非法值（threshold 非 0~1 的有限数 / min_interval ≤ 0）抛 ValueError。"""
    kwargs: dict = {}
    ov = overrides or {}
    if "threshold" in ov:
        t = _as_float(ov["threshold"], "threshold")
        if not math.isfinite(t) or not 0.0 <= t <= 1.0:
            raise ValueError(f"threshold 必须为 0~1 的有限数: {ov['threshold']!r}")
        kwargs["threshold"] = t
    if "min_interval" in ov:
        mi = _as_float(ov["min_interval"], "min_interval")
        if not mi > 0:
            raise ValueError(f"min_interval 必须 > 0: {ov['min_interval']!r}")
        kwargs["min_interval"] = mi
    if "sample_period" in ov:
        sp = _as_float(ov["sample_period"], "sample_period")
        if not sp > 0:
            raise ValueError(f"sample_period 必须 > 0: {ov['sample_period']!r}")
        kwargs["sample_period"] = sp
    if "stealth_default" in ov:
        if not isinstance(ov["stealth_default"], bool):
            raise ValueError(f"stealth_default 必须为 bool: {ov['stealth_default']!r}")
        kwargs["stealth_default"] = ov["stealth_default"]
    return MoodConfig(**kwargs)
