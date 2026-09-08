"""隐身模式测试（mood-sync impl §6.3）。"""

from __future__ import annotations

from sync.events import EventType

from core.mood_export import Emotion
from core.mood_privacy import MoodPrivacy
from core.mood_receive import MoodReceiver
from core.mood_sender import MoodSender


class FakeSync:
    """记录 send 调用的假 sync（鸭子类型）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[EventType, dict]] = []

    def send(self, event_type, payload, **kwargs) -> None:
        self.calls.append((event_type, payload))


def test_default_off():
    sync = FakeSync()
    p = MoodPrivacy(sync, MoodReceiver())
    assert p.is_stealth is False  # 默认关闭


def test_enable_sets_flag():
    sync = FakeSync()
    p = MoodPrivacy(sync, MoodReceiver())
    p.set_stealth(True)
    assert p.is_stealth is True


def test_enable_sends_privacy_notification():
    sync = FakeSync()
    p = MoodPrivacy(sync, MoodReceiver())
    p.set_stealth(True)
    assert len(sync.calls) == 1
    assert sync.calls[0][0] == EventType.MOOD_SYNC
    assert sync.calls[0][1] == {"privacy": True}  # 无任何情绪键
    assert set(sync.calls[0][1].keys()) == {"privacy"}


def test_enable_clears_partner_state():
    sync = FakeSync()
    events: list = []
    recv = MoodReceiver(on_mood_change=events.append)
    recv.handle({"valence": 0.8, "arousal": 0.6, "label": "happy"})
    assert recv.partner_mood() is not None
    p = MoodPrivacy(sync, recv)
    p.set_stealth(True)
    assert recv.partner_mood() is None  # 本端已展示的对方状态被清除
    assert recv.partner_stealth() is False
    assert events and events[-1].stealth is False


def test_enable_idempotent():
    sync = FakeSync()
    p = MoodPrivacy(sync, MoodReceiver())
    p.set_stealth(True)
    p.set_stealth(True)  # 重复开启
    assert len(sync.calls) == 1  # 仅发一条 privacy 通知


def test_disable_resets_without_send():
    sync = FakeSync()
    p = MoodPrivacy(sync, MoodReceiver())
    p.set_stealth(True)
    assert p.is_stealth is True
    p.set_stealth(False)
    assert p.is_stealth is False
    assert len(sync.calls) == 1  # 关闭不发任何消息（仅开启时的 1 条通知）
    p.set_stealth(False)  # 重复关闭幂等
    assert len(sync.calls) == 1


def test_sender_linkage():
    """隐身中 maybe_send 返回 False 不发送；关闭后恢复正常。"""
    sync = FakeSync()
    recv = MoodReceiver()
    privacy = MoodPrivacy(sync, recv)
    # is_stealth 为属性 → 用 lambda 包装为查询回调（C1/C2 接线同此）
    sender = MoodSender(sync, is_stealth=lambda: privacy.is_stealth)
    assert sender.maybe_send(Emotion(0.8, 0.6, "happy"), now=0) is True
    privacy.set_stealth(True)
    assert sender.maybe_send(Emotion(-0.5, 0.8, "sad"), now=1000) is False  # 隐身拦截
    privacy.set_stealth(False)
    assert sender.maybe_send(Emotion(-0.5, 0.8, "sad"), now=1000) is True  # 关闭后恢复
    # 调用序列：happy 帧 → privacy 通知（仅 1 键）→ sad 帧
    assert len(sync.calls) == 3
    assert set(sync.calls[0][1].keys()) == {"valence", "arousal", "label"}
    assert sync.calls[1][1] == {"privacy": True}
    assert set(sync.calls[2][1].keys()) == {"valence", "arousal", "label"}
