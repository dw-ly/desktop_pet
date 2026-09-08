"""接收与表现映射测试（mood-sync impl §5.2）。"""

from __future__ import annotations

from core.mood_config import MOOD_ANIMATION, MOOD_LABELS
from core.mood_export import Emotion
from core.mood_receive import MoodEvent, MoodReceiver


def _frame(label: str, valence: float = 0.5, arousal: float = 0.5) -> dict:
    return {"valence": valence, "arousal": arousal, "label": label}


def test_all_labels_map_to_animation():
    for label in MOOD_LABELS:
        r = MoodReceiver()
        assert r.handle(_frame(label)) is True
        ov = r.animation_override()
        assert ov == MOOD_ANIMATION[label]
        assert ov is not None


def test_receive_happy():
    r = MoodReceiver()
    assert r.handle(_frame("happy", 0.8, 0.6)) is True
    assert r.partner_mood().label == "happy"
    assert r.animation_override() == "idle_happy"


def test_local_interaction_priority():
    r = MoodReceiver()
    r.handle(_frame("happy", 0.8, 0.6))
    assert r.animation_override(local_active=True) is None  # 本地交互不覆盖
    assert r.animation_override(local_active=False) == "idle_happy"


def test_initial_no_label():
    r = MoodReceiver()
    assert r.partner_mood() is None
    assert r.partner_stealth() is False
    assert r.animation_override() is None


def test_privacy_notification():
    r = MoodReceiver()
    r.handle(_frame("happy", 0.8, 0.6))
    assert r.handle({"privacy": True}) is True
    assert r.partner_stealth() is True
    assert r.partner_mood().label == "happy"  # partner_mood 保留原值


def test_label_preserved_after_privacy():
    r = MoodReceiver()
    r.handle(_frame("happy", 0.8, 0.6))
    r.handle({"privacy": True})
    assert r.animation_override() == "idle_happy"  # 保留最近表现，稳定不闪烁


def test_invalid_payload():
    r = MoodReceiver()
    r.handle(_frame("happy", 0.8, 0.6))
    assert r.handle({"valence": "x", "arousal": 0.6, "label": "happy"}) is False
    assert r.handle({"valence": 0.8}) is False  # 缺字段
    assert r.handle({"valence": 2.0, "arousal": 0.6, "label": "happy"}) is False
    assert r.handle({"privacy": False}) is False  # privacy=false 非法（非通知非情绪帧）
    assert r.partner_mood().label == "happy"  # 状态不变
    assert r.partner_stealth() is False


def test_clear_partner_state():
    events: list[MoodEvent] = []
    r = MoodReceiver(on_mood_change=events.append)
    r.handle(_frame("happy", 0.8, 0.6))
    r.clear_partner_state()
    assert r.partner_mood() is None
    assert r.partner_stealth() is False
    assert events[-1] == MoodEvent(None, False)  # 触发回调


def test_callback_payload():
    events: list[MoodEvent] = []
    r = MoodReceiver(on_mood_change=events.append)
    r.handle(_frame("sleepy", 0.2, 0.1))
    assert events[-1] == MoodEvent(Emotion(0.2, 0.1, "sleepy"), False)
    r.handle({"privacy": True})
    assert events[-1] == MoodEvent(Emotion(0.2, 0.1, "sleepy"), True)  # 保留最近标签


def test_emotion_frame_resets_stealth():
    r = MoodReceiver()
    r.handle({"privacy": True})
    assert r.partner_stealth() is True
    r.handle(_frame("happy", 0.8, 0.6))
    assert r.partner_stealth() is False  # 情绪帧复位隐身标记
    assert r.partner_mood().label == "happy"


def test_float_normalization():
    r = MoodReceiver()
    r.handle({"valence": 1, "arousal": 0, "label": "excited"})  # int 输入
    m = r.partner_mood()
    assert isinstance(m.valence, float)
    assert isinstance(m.arousal, float)
