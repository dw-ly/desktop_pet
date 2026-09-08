"""祝福生成测试（anniversary impl §4.1 / plan S2）。"""

from __future__ import annotations

import time

import pytest

from core.anniv_blessing import AnnivBlessing
from core.anniv_config import BLESSING_TEMPLATES, load_anniv_config

TITLE = "在一起纪念日"


def _rendered(style: str) -> set[str]:
    return {t.format(title=TITLE) for t in BLESSING_TEMPLATES[style]}


def test_no_llm_falls_back_to_template():
    b = AnnivBlessing()
    text = b.generate(TITLE)
    assert text in _rendered("cute")
    assert TITLE in text


def test_llm_success():
    b = AnnivBlessing(llm=lambda prompt: "小团子永远爱你！")
    assert b.generate(TITLE) == "小团子永远爱你！"


def test_llm_raises_falls_back():
    def bad(_prompt):
        raise RuntimeError("LLM 挂了")

    b = AnnivBlessing(llm=bad)
    text = b.generate(TITLE)
    assert text in _rendered("cute")


def test_llm_timeout_falls_back():
    def slow(_prompt):
        time.sleep(0.5)
        return "太慢的文案"

    cfg = load_anniv_config({"blessing_timeout": 0.1})
    b = AnnivBlessing(cfg, llm=slow)
    t0 = time.time()
    text = b.generate(TITLE)
    assert time.time() - t0 < 0.4  # 未等慢 LLM 完成
    assert text in _rendered("cute")


def test_llm_empty_falls_back():
    b = AnnivBlessing(llm=lambda prompt: "   ")
    assert b.generate(TITLE) in _rendered("cute")


def test_style_switch():
    b = AnnivBlessing()
    cute = b.generate(TITLE, style="cute")
    deep = b.generate(TITLE, style="deep")
    assert cute in _rendered("cute")
    assert deep in _rendered("deep")


def test_invalid_style_falls_back_to_cute():
    b = AnnivBlessing()
    assert b.generate(TITLE, style="x") in _rendered("cute")


def test_default_style_from_config():
    cfg = load_anniv_config({"default_blessing_style": "deep"})
    b = AnnivBlessing(cfg)
    assert b.generate(TITLE) in _rendered("deep")
