"""纪念日配置（anniversary impl §2.1 / plan G1）。

- `AnnivConfig`：限时装扮时长/祝福风格/默认值等可调项（frozen dataclass）。
- `DEFAULT_PRESETS`：常用纪念日预设（spec §3.3.1"默认提供常用类型"）。
- `BLESSING_STYLES` / `BLESSING_TEMPLATES`：祝福风格与模板兜底文案（S2 用）。
"""

from __future__ import annotations

from dataclasses import dataclass

BLESSING_STYLES: tuple[str, ...] = ("cute", "deep")

# 默认常用纪念日预设（title/calendar/repeat 三字段，date 由用户添加时填写）
DEFAULT_PRESETS: tuple[dict, ...] = (
    {"title": "在一起纪念日", "calendar": "solar", "repeat": "yearly"},
    {"title": "对方生日", "calendar": "solar", "repeat": "yearly"},
    {"title": "第一次约会", "calendar": "solar", "repeat": "yearly"},
    {"title": "七夕", "calendar": "lunar", "repeat": "yearly"},
)

# 祝福模板（{title} 占位，S2 generate 随机取一渲染）
BLESSING_TEMPLATES: dict[str, tuple[str, ...]] = {
    "cute": (
        "{title}到啦，团子想和你贴贴！",
        "今天是{title}，抱抱我的团子～",
    ),
    "deep": (
        "{title}，谢谢你陪我走到今天。",
        "{title}，在一起的每一天都值得珍藏。",
    ),
}


@dataclass(frozen=True)
class AnnivConfig:
    outfit_item_id: str = "anniv_limited_suit"   # 限时装扮 item_id（D34）
    outfit_duration_days: int = 1                # 限时装扮时长：today + N 日零点失效
    blessing_timeout: float = 3.0                # LLM 生成超时秒数（spec §4.1）
    default_blessing_style: str = "cute"         # 默认祝福风格
    default_calendar: str = "solar"
    default_repeat: str = "yearly"
    default_notify_days: int = 1                 # 添加纪念日默认提前提醒天数


def load_anniv_config(overrides: dict | None = None) -> AnnivConfig:
    """配置加载：缺失键用默认值；overrides 仅覆盖给定键。

    非法值（outfit_duration_days ≤ 0 / blessing_timeout ≤ 0 / default_notify_days < 0 /
    默认风格/历法/重复非法）抛 ValueError。
    """
    cfg = AnnivConfig(**(overrides or {}))
    if cfg.outfit_duration_days <= 0:
        raise ValueError("outfit_duration_days 需 > 0")
    if cfg.blessing_timeout <= 0:
        raise ValueError("blessing_timeout 需 > 0")
    if cfg.default_notify_days < 0:
        raise ValueError("default_notify_days 需 ≥ 0")
    if cfg.default_blessing_style not in BLESSING_STYLES:
        raise ValueError(f"default_blessing_style 非法: {cfg.default_blessing_style!r}")
    if cfg.default_calendar not in ("solar", "lunar"):
        raise ValueError(f"default_calendar 非法: {cfg.default_calendar!r}")
    if cfg.default_repeat not in ("once", "yearly"):
        raise ValueError(f"default_repeat 非法: {cfg.default_repeat!r}")
    return cfg
