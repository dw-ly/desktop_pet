"""带话意图检测测试（carry-message impl §2.3）。"""

from __future__ import annotations

from core.carry_intent import detect_carry


def test_prefix_short_trigger():
    r = detect_carry("告诉TA今晚早点睡")
    assert r.is_carry is True
    assert r.text == "今晚早点睡"  # 前导称呼 "ta" 一并剥离
    assert r.trigger == "告诉"


def test_prefix_long_trigger_priority():
    r = detect_carry("帮我告诉TA我加班")
    assert r.is_carry is True
    assert r.trigger == "帮我告诉"  # 长词优先，而非 "告诉"
    assert r.text == "我加班"


def test_gen_ta_say_case_space():
    # 无空格输入
    r1 = detect_carry("跟TA说我想TA了")
    assert r1.is_carry is True
    assert r1.text == "我想ta了"  # 正文中部 ta 保留
    # 带空格输入（"跟 ta 说" 变体）：触发命中，正文保留单空格
    r2 = detect_carry("跟 TA 说我想 TA 了")
    assert r2.is_carry is True
    assert r2.trigger == "跟 ta 说"
    assert r2.text == "我想 ta 了"


def test_dui_ta_say():
    r = detect_carry("对ta说晚安吧")
    assert r.is_carry is True
    assert r.text == "晚安"  # 剥尾部 "吧"


def test_trailer_strip():
    r = detect_carry("告诉TA快睡觉呀")
    assert r.is_carry is True
    assert r.text == "快睡觉"


def test_not_carry_normal_chat():
    r = detect_carry("今天天气不错")
    assert r.is_carry is False
    assert r.text == ""
    assert r.trigger is None


def test_mid_sentence_not_hit():
    r = detect_carry("我想告诉TA这个秘密")
    assert r.is_carry is False  # 句首规则，句中触发词不命中


def test_empty_input():
    for s in ("", "   "):
        r = detect_carry(s)
        assert r.is_carry is False
        assert r.text == ""


def test_empty_body_fallback():
    r = detect_carry("告诉")
    assert r.is_carry is True
    assert r.text == "告诉"  # 空正文兜底返回触发词本身


def test_trigger_with_trailer_fallback():
    r = detect_carry("告诉吧")
    assert r.is_carry is True
    assert r.text == "告诉"  # 剥语气词后为空 → 兜底触发词
