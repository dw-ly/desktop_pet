"""节流与发送测试（mood-sync impl §4.3）。"""

from __future__ import annotations

from sync.events import EventType

from core.mood_config import MoodConfig
from core.mood_export import Emotion
from core.mood_sender import MoodSender


class FakeSync:
    """记录 send 调用的假 sync（鸭子类型）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[EventType, dict]] = []

    def send(self, event_type, payload, **kwargs) -> None:
        self.calls.append((event_type, payload))


def test_first_send_always():
    sync = FakeSync()
    s = MoodSender(sync)
    assert s.maybe_send(Emotion(0.8, 0.6, "happy"), now=0) is True
    assert len(sync.calls) == 1
    assert sync.calls[0][0] == EventType.MOOD_SYNC
    assert sync.calls[0][1] == {"valence": 0.8, "arousal": 0.6, "label": "happy"}


def test_no_significant_change():
    sync = FakeSync()
    s = MoodSender(sync)
    assert s.maybe_send(Emotion(0.8, 0.6, "happy"), now=0) is True
    # 间隔足够（now=1000 > 600），但 Δ 均小于阈值 → 不发送
    assert s.maybe_send(Emotion(0.7, 0.4, "calm"), now=1000) is False
    assert len(sync.calls) == 1


def test_exact_threshold_triggers():
    sync = FakeSync()
    s = MoodSender(sync)
    assert s.maybe_send(Emotion(0.8, 0.6, "happy"), now=0) is True
    # |Δvalence| = 0.3 恰在阈值 → 触发（≥ 边界）
    assert s.maybe_send(Emotion(0.5, 0.6, "neutral"), now=1000) is True
    assert len(sync.calls) == 2


def test_interval_insufficient():
    sync = FakeSync()
    s = MoodSender(sync)
    assert s.maybe_send(Emotion(0.8, 0.6, "happy"), now=0) is True
    # 显著变化（|Δvalence|=0.9）但间隔 < 600 → 不发送
    assert s.maybe_send(Emotion(-0.1, 0.6, "sad"), now=599) is False
    assert len(sync.calls) == 1


def test_exact_interval_allows():
    sync = FakeSync()
    s = MoodSender(sync)
    assert s.maybe_send(Emotion(0.8, 0.6, "happy"), now=0) is True
    # now - last = 600 恰好满足最小间隔，且变化显著 → 发送
    assert s.maybe_send(Emotion(-0.1, 0.6, "sad"), now=600) is True
    assert len(sync.calls) == 2


def test_stealth_blocks():
    sync = FakeSync()
    s = MoodSender(sync, is_stealth=lambda: True)
    assert s.maybe_send(Emotion(0.8, 0.6, "happy"), now=0) is False
    assert len(sync.calls) == 0
    # 关闭隐身 → 恢复
    s2 = MoodSender(sync, is_stealth=lambda: False)
    assert s2.maybe_send(Emotion(0.8, 0.6, "happy"), now=0) is True


def test_payload_only_three_fields():
    sync = FakeSync()
    s = MoodSender(sync)
    s.maybe_send(Emotion(0.8, 0.6, "happy"), now=0)
    payload = sync.calls[0][1]
    assert set(payload.keys()) == {"valence", "arousal", "label"}
    # 无任何文本键
    assert "text" not in payload
    assert "message" not in payload
    assert all(isinstance(v, float | int) for k, v in payload.items() if k != "label")


def test_record_after_send_and_reset():
    sync = FakeSync()
    s = MoodSender(sync)
    assert s.maybe_send(Emotion(0.8, 0.6, "happy"), now=0) is True
    # 下次同值（Δ<0.3）→ 不再发送
    assert s.maybe_send(Emotion(0.8, 0.6, "happy"), now=1000) is False
    assert len(sync.calls) == 1
    # reset 后恢复首次语义
    s.reset()
    assert s.maybe_send(Emotion(0.8, 0.6, "happy"), now=2000) is True
    assert len(sync.calls) == 2


def test_invalid_emotion_rejected():
    sync = FakeSync()
    s = MoodSender(sync)
    assert s.maybe_send(Emotion(1.5, 0.6, "happy"), now=0) is False
    assert len(sync.calls) == 0


def test_custom_config_threshold():
    sync = FakeSync()
    s = MoodSender(sync, config=MoodConfig(threshold=0.5, min_interval=60.0))
    assert s.maybe_send(Emotion(0.8, 0.6, "happy"), now=0) is True
    # Δ=0.1 < 0.5 且 Δarousal=0 → 不发送
    assert s.maybe_send(Emotion(0.7, 0.6, "calm"), now=100) is False
    assert len(sync.calls) == 1
