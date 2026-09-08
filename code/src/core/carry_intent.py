"""带话意图检测与正文抽取（对应 carry-message impl §2 / plan S1）。

- 纯规则、无外部依赖：句首触发词匹配 → 剥离前导对端称呼 "ta" → 剥离尾部语气词
- 未命中返回 is_carry=False（走普通对话）
- 空正文兜底返回触发词本身，避免发送空 text
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class IntentResult:
    """意图检测结果。"""

    is_carry: bool
    text: str                 # 规范化正文（小写、压缩空白、剥离前导称呼与尾部语气词）；非带话为空串
    trigger: str | None       # 命中的触发词；未命中 None


# 长词在前（"帮我告诉" 优先于 "告诉"），否则短词会截断长词。
# "跟 TA 说"（带空格）与 "跟TA说"（无空格）经 _norm 归一为 "跟 ta 说"/"跟ta说"，
# 故 "跟/对 + ta" 类触发词同时收录带空格与不带空格两个变体。
TRIGGERS: tuple[str, ...] = (
    "帮我告诉", "帮我转告", "帮我传话", "帮我带句话",
    "跟 ta 说", "跟ta说", "对 ta 说", "对ta说", "跟他说", "跟她说",
    "告诉", "转告", "传话",
)

_TRAILERS = ("吧", "呀", "哦", "呢", "哟", "啊")


def _norm(s: str) -> str:
    """小写 + 压缩空白 + 去首尾空白。TA/ta 大小写经 lower 统一，不必特判。"""
    return re.sub(r"\s+", " ", s.strip().lower())


def _strip_leading_ta(s: str) -> str:
    """剥离正文开头的对端称呼 "ta"（"告诉TA今晚早点睡"→"今晚早点睡"）。

    仅精确匹配前导 2 字符 "ta"；"ta们" 等特殊形态罕见，不特判。
    """
    return s[2:].strip() if s.startswith("ta") else s


def detect_carry(text: str) -> IntentResult:
    """句首触发词匹配 + 正文抽取；未命中按普通对话。"""
    t = _norm(text)
    if not t:
        return IntentResult(False, "", None)
    for trig in TRIGGERS:
        if t.startswith(trig):
            body = _strip_leading_ta(t[len(trig):].strip())
            while body and body[-1] in _TRAILERS:
                body = body[:-1].strip()
            # 空正文兜底：返回触发词本身（避免发送空内容）
            return IntentResult(True, body or trig, trig)
    return IntentResult(False, "", None)
