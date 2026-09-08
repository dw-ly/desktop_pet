"""共同养成 成长配置与解锁映射（对应 pet-growth impl §2 / plan G1）。

- `PetConfig`：积分来源表数值（唯一来源，spec §3.3.2）、等级曲线 k、里程碑、
  灰色期参数、带话频率门控。每日 reason 上限**不在此表**——引用
  consistency `INTIMACY_DAILY_LIMITS`（chat=1/feed=5/streak=1），本模块不重复定义。
- `UNLOCK_ITEMS`：等级/亲密度里程碑 → 解锁物品 id 的配置表（spec 决议 5/10/15
  级 + 100/365/1000 亲密度；04 号笔记早期清单已废弃，以本表为准）。
- `level_from_intimacy`：等级曲线 level = int(sqrt(intimacy/k))（D26：exp 镜像
  亲密度，不单独存 exp；level 从 0 起算）。
- `ACTIVITY_TYPES`：参与"共同登录"判定的业务事件类型（D24，排除 date.sync 防
  对齐自触发）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# 解锁映射（item_id → 元数据；kind: level|intimacy；threshold: 达到即解锁）
# --------------------------------------------------------------------------- #

UNLOCK_ITEMS: dict[str, dict] = {
    "unlock_level_5": {
        "name": "普通装扮",
        "type": "outfit",
        "rarity": "common",
        "kind": "level",
        "threshold": 5,
    },
    "unlock_level_10": {
        "name": "专属动作",
        "type": "action",
        "rarity": "uncommon",
        "kind": "level",
        "threshold": 10,
    },
    "unlock_level_15": {
        "name": "高级装扮",
        "type": "outfit",
        "rarity": "rare",
        "kind": "level",
        "threshold": 15,
    },
    "unlock_intimacy_100": {
        "name": "爱心气泡",
        "type": "bubble",
        "rarity": "event",
        "kind": "intimacy",
        "threshold": 100,
    },
    "unlock_intimacy_365": {
        "name": "纪念日套装",
        "type": "outfit",
        "rarity": "anniversary",
        "kind": "intimacy",
        "threshold": 365,
    },
    "unlock_intimacy_1000": {
        "name": "羁绊徽章",
        "type": "badge",
        "rarity": "legend",
        "kind": "intimacy",
        "threshold": 1000,
    },
}

# 参与"共同登录"判定的业务事件类型（date.sync 为对齐自触发，排除防自我激活，D24）
ACTIVITY_TYPES: frozenset[str] = frozenset(
    {
        "msg.carry",
        "carry.ack",
        "carry.revoke",
        "mood.sync",
        "pet.feed",
        "pet.profile",
        "date.add",
        "date.remind",
        "gift.send",
        "gift.accept",
        "gift.expire",
    }
)


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PetConfig:
    """共同养成配置（积分来源表数值与 spec §3.3.2 完全一致）。"""

    level_k: int = 100                # 等级曲线 k：level = int(sqrt(intimacy/k))
    feed_delta: int = 3               # 日常喂食/摸头基础积分
    carry_delta: int = 10             # 带话送达基础积分
    chat_delta: int = 5               # 共同对话（当日首次）基础积分
    gift_delta: int = 5               # 礼物接受基础积分
    gift_anniversary_delta: int = 20  # 礼物接受纪念日特惠（D29，不叠加 ×2）
    carry_min_interval: float = 300.0  # 带话频率门控：距上次 ≥ 此秒数才计（D28）
    level_milestones: tuple[int, ...] = (5, 10, 15)
    intimacy_milestones: tuple[int, ...] = (100, 365, 1000)
    grace_days: int = 3               # 灰色期容错天数（漏登 -1，耗尽清零）
    grace_recover_days: int = 3       # 灰色期连续共同登录日 ≥ 此值恢复 normal


def _validate(cfg: PetConfig) -> None:
    """非法值抛 ValueError。"""
    for field, value in (
        ("level_k", cfg.level_k),
        ("feed_delta", cfg.feed_delta),
        ("carry_delta", cfg.carry_delta),
        ("chat_delta", cfg.chat_delta),
        ("gift_delta", cfg.gift_delta),
        ("gift_anniversary_delta", cfg.gift_anniversary_delta),
    ):
        if not isinstance(value, int) or value <= 0:
            raise ValueError(f"{field} 必须为正整数，得到 {value!r}")
    if (
        not isinstance(cfg.carry_min_interval, (int, float))
        or cfg.carry_min_interval <= 0
    ):
        raise ValueError(f"carry_min_interval 必须为正数，得到 {cfg.carry_min_interval!r}")
    if not isinstance(cfg.grace_days, int) or cfg.grace_days < 0:
        raise ValueError(f"grace_days 必须为非负整数，得到 {cfg.grace_days!r}")
    if not isinstance(cfg.grace_recover_days, int) or cfg.grace_recover_days <= 0:
        raise ValueError(
            f"grace_recover_days 必须为正整数，得到 {cfg.grace_recover_days!r}"
        )
    for field in ("level_milestones", "intimacy_milestones"):
        ms = getattr(cfg, field)
        if not isinstance(ms, tuple) or not ms:
            raise ValueError(f"{field} 必须为非空元组")
        prev = 0
        for v in ms:
            if not isinstance(v, int) or v <= prev:
                raise ValueError(f"{field} 必须为正且严格递增")
            prev = v


def load_pet_config(overrides: dict | None = None) -> PetConfig:
    """配置加载：缺失键用默认值；overrides 仅覆盖给定键。非法值抛 ValueError。

    里程碑字段接受 tuple/list（list 自动转 tuple，保证 dataclass 不可变）。
    """
    if overrides:
        base = {
            f.name: getattr(PetConfig, f.name)
            for f in PetConfig.__dataclass_fields__.values()
        }
        for key, value in overrides.items():
            if key in ("level_milestones", "intimacy_milestones") and isinstance(
                value, (list, tuple)
            ):
                value = tuple(value)
            base[key] = value
        cfg = PetConfig(**base)
    else:
        cfg = PetConfig()
    _validate(cfg)
    return cfg


# --------------------------------------------------------------------------- #
# 等级曲线与解锁判定
# --------------------------------------------------------------------------- #

def level_from_intimacy(intimacy: int, k: int | None = None) -> int:
    """level = int(sqrt(intimacy / k))，k 默认 100（D26）。intimacy ≤ 0 → 0。"""
    if intimacy <= 0:
        return 0
    return int(math.sqrt(intimacy / (k if k is not None else 100)))


def earned_unlock_ids(cfg: PetConfig, level: int, intimacy: int) -> set[str]:
    """返回达到阈值应解锁的全部 item_id（kind=level 取 threshold ≤ level；
    kind=intimacy 取 threshold ≤ intimacy）。"""
    earned: set[str] = set()
    for item_id, meta in UNLOCK_ITEMS.items():
        threshold = int(meta["threshold"])
        if meta["kind"] == "level" and level >= threshold:
            earned.add(item_id)
        elif meta["kind"] == "intimacy" and intimacy >= threshold:
            earned.add(item_id)
    return earned
