"""礼物 UI 纯助手测试（gift-exchange C1）。"""

from __future__ import annotations

from pathlib import Path

from core.gift_config import GiftItem, GiftState, load_gift_config
from ui.gift_dialog import (
    confirm_copy,
    egg_validation_hint,
    group_catalog,
    receive_prompt,
    resend_hint,
    status_label,
    type_label,
    unlock_success_text,
)

REPO_MANIFEST = Path(__file__).resolve().parents[3] / "assets" / "manifest.json"


def test_group_catalog():
    cfg = load_gift_config(manifest_path=REPO_MANIFEST)
    groups = group_catalog(cfg.catalog)
    assert set(groups) == {"outfit", "action", "emoji", "custom_egg"}
    assert len(groups["outfit"]) == 7
    assert len(groups["action"]) == 5
    assert len(groups["emoji"]) == 5
    assert groups["custom_egg"] == []


def test_confirm_and_receive_copy():
    item = GiftItem("outfit-heart-100", "爱心气泡装扮", "outfit", "event", 30)
    text = confirm_copy(item)
    assert "爱心气泡装扮" in text
    assert "不可撤回" in text
    assert "🎁" in receive_prompt()
    assert "爱心" in receive_prompt("爱心气泡装扮")
    assert "已解锁" in unlock_success_text("爱心气泡装扮")


def test_status_labels():
    assert status_label(GiftState.SENT) == "已发送"
    assert status_label(GiftState.ACCEPTED) == "已接受"
    assert status_label(GiftState.EXPIRED) == "已过期退回"
    assert status_label("sent") == "已发送"
    assert resend_hint(GiftState.EXPIRED) is not None
    assert resend_hint(GiftState.SENT) is None


def test_egg_validation_hint():
    assert egg_validation_hint("") is not None
    assert egg_validation_hint("hi") is None
    assert egg_validation_hint("x" * 201) is not None


def test_type_label():
    assert type_label("outfit") == "装扮"
    assert type_label("custom_egg") == "自定义彩蛋"
