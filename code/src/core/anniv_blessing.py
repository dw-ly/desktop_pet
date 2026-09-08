"""祝福生成（anniversary impl §4 / plan S2）。

主项目 LLM 注入（`llm: Callable[[str], str]`）+ 超时/失败/空白模板兜底 + 风格（卖萌/深情）。
未注入 LLM 时恒模板（测试/无模型场景），不阻塞提醒。
"""

from __future__ import annotations

import random
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Callable

from .anniv_config import (
    BLESSING_STYLES,
    BLESSING_TEMPLATES,
    AnnivConfig,
    load_anniv_config,
)

_STYLE_DESC = {"cute": "卖萌可爱", "deep": "深情走心"}


class AnnivBlessing:
    """祝福生成：LLM 注入 + 超时/失败模板兜底 + 风格切换。"""

    def __init__(self, cfg: AnnivConfig | None = None,
                 llm: Callable[[str], str] | None = None) -> None:
        self._cfg = cfg or load_anniv_config()
        self._llm = llm

    def build_prompt(self, title: str, style: str, today: date) -> str:
        return (
            f"今天是{title}（{today.isoformat()}）。请用团子口吻写一句不超过 20 字的"
            f"祝福语，风格：{_STYLE_DESC.get(style, _STYLE_DESC['cute'])}。"
            "只输出祝福语本身，不要任何前缀。"
        )

    def generate(self, title: str, style: str | None = None,
                 today: date | None = None) -> str:
        """返回非空祝福文案（LLM 或模板兜底）。"""
        style = style or self._cfg.default_blessing_style
        if style not in BLESSING_STYLES:
            style = "cute"
        if self._llm is None:
            return self._fallback(title, style)
        today = today or date.today()
        prompt = self.build_prompt(title, style, today)
        text: str | None
        try:
            ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="anniv-llm")
            fut = ex.submit(self._llm, prompt)
            try:
                text = fut.result(timeout=self._cfg.blessing_timeout)
            except Exception:
                fut.cancel()
                text = None
            finally:
                ex.shutdown(wait=False)  # 慢线程后台结束，不阻塞调用方
        except Exception:  # 极罕见：executor 创建失败
            text = None
        if not text or not text.strip():
            text = self._fallback(title, style)
        return text.strip()

    @staticmethod
    def _fallback(title: str, style: str) -> str:
        return random.choice(BLESSING_TEMPLATES[style]).format(title=title)
