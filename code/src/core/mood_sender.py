"""节流判定 + mood.sync 发送（对应 mood-sync impl §4 / plan S2）。

节流算法（impl §4.1，spec §3.3.2）：
  1. 隐身中（is_stealth() 为 True）→ 不发送               ← 最高优先级（spec §3.4）
  2. now - last_sync_ts < min_interval → 不发送            ← 最小间隔
  3. last_sent 存在 且 |Δvalence| < threshold 且 |Δarousal| < threshold → 不发送 ← 无显著变化
  4. 通过 → 发送 payload（仅三字段）+ 记录 → 返回 True
  首次（last_sent 为 None）：跳过第 3 步直接发送（决策 D21）。

内存态记录上次发送值/时间（不持久化）。时间源可注入（测试可控），生产默认 time.time()。
"""

from __future__ import annotations

import time
from typing import Callable

from core.mood_config import MoodConfig
from core.mood_export import Emotion, valid_mood_values
from sync.events import EventType


class MoodSender:
    """节流判定 + mood.sync 发送。内存态记录上次发送值/时间（不持久化）。"""

    def __init__(
        self,
        sync,
        config: MoodConfig | None = None,
        is_stealth: Callable[[], bool] | None = None,
    ) -> None:
        # sync: SyncManager 鸭子类型（仅 send），不依赖具体类型
        # is_stealth: 查询隐身状态的回调（由 S4 MoodPrivacy 提供），缺省恒 False
        self._sync = sync
        self._config = config or MoodConfig()
        self._is_stealth = is_stealth or (lambda: False)
        self._last_sent: Emotion | None = None
        self._last_sync_ts: float | None = None

    def maybe_send(self, emotion: Emotion, now: float | None = None) -> bool:
        """按 §4.1 算法节流判定并发送。发送负载 = {"valence", "arousal", "label"} 三字段。"""
        if not valid_mood_values(emotion.valence, emotion.arousal, emotion.label):
            return False  # 本端不发非法负载（防线与 MoodExporter 一致）
        if self._is_stealth():
            return False
        ts = time.time() if now is None else now
        if self._last_sync_ts is not None and ts - self._last_sync_ts < self._config.min_interval:
            return False
        if self._last_sent is not None:
            dv = abs(emotion.valence - self._last_sent.valence)
            da = abs(emotion.arousal - self._last_sent.arousal)
            if dv < self._config.threshold and da < self._config.threshold:
                return False
        self._sync.send(
            EventType.MOOD_SYNC,
            {"valence": emotion.valence, "arousal": emotion.arousal, "label": emotion.label},
        )
        self._last_sent = emotion
        self._last_sync_ts = ts
        return True

    def reset(self) -> None:
        """清空 last_sent / last_sync_ts（换机/重配对场景可选调用，下次发送视为首次）。"""
        self._last_sent = None
        self._last_sync_ts = None
