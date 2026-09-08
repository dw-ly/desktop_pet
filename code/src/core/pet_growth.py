"""共同养成 积分计算与本地合入（对应 pet-growth impl §3 / plan S1）。

- `PetGrowth.award`：本地互动行为唯一入口——delta 计算（纪念日钩子加成）→
  门控（carry 5min，D28；其余 reason 由 apply_intimacy_event 每日上限兜底）→
  MAC 签名 → `apply_intimacy_event` 本地乐观合入 → `sync.send(PET_FEED)`。
  合入失败（超上限/验签失败/熔断）返回 0 且**不发送**（防作弊一致性）。
- `delta_for`：积分计算纯函数（不落库、不发），供测试/UI 预演。
- `set_anniversary_hook` / `is_anniversary_today`：纪念日可注入钩子（D27），
  anniversary 模块落地后注册，未注册恒 False。

积分规则（spec §3.3.2 唯一来源表 + 纪念日统一规则，impl §3.1）：
  gift：特惠 20（纪念日）/ 普通 5，不叠加 ×2（D29）
  其它：基础值（carry 10 / chat 5 / feed 3 / streak 显式传 delta=streak×2），
        纪念日 ×2 取整
"""

from __future__ import annotations

import time
from typing import Callable

from sync.events import EventType

from .consistency import INTIMACY_REASONS, apply_intimacy_event, sign_intimacy_event
from .pet_config import PetConfig, load_pet_config

_KV_CARRY_LAST = "pet:carry_last_ts"

# --------------------------------------------------------------------------- #
# 纪念日钩子（D27）
# --------------------------------------------------------------------------- #

_HOOK: Callable[[], bool] = lambda: False


def set_anniversary_hook(fn: Callable[[], bool]) -> None:
    """注册纪念日判定钩子（anniversary 模块落地后调用）；未注册恒 False。"""
    global _HOOK
    _HOOK = fn


def is_anniversary_today() -> bool:
    """纪念日当天返回 True（anniversary 模块注册后）；未注册返回 False。"""
    return bool(_HOOK())


# --------------------------------------------------------------------------- #
# kv 辅助（pet_state/kv 读写）
# --------------------------------------------------------------------------- #

def _kv_get(db, key: str) -> str | None:
    row = db.query_one("SELECT value FROM kv WHERE key=?", (key,))
    return str(row["value"]) if row else None


def _kv_set(db, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


# --------------------------------------------------------------------------- #
# 积分计算与合入
# --------------------------------------------------------------------------- #

class PetGrowth:
    """本地互动行为入口 → delta 计算 → 门控 → 签名 → 本地乐观合入 → 发送。"""

    def __init__(
        self,
        db,
        sync,
        mac_key: bytes,
        cfg: PetConfig | None = None,
    ) -> None:
        self._db = db
        self._sync = sync  # SyncManager 鸭子类型（仅 send）
        self._mac_key = mac_key
        self._cfg = cfg or load_pet_config()

    # -- 积分计算（纯函数，impl §3.1） -- #

    def delta_for(self, reason: str, *, delta: int | None = None) -> int:
        """按 3.1 规则计算 delta（不落库、不发）。

        - reason == 'gift'：特惠 20（纪念日）/ 普通 5（不叠加 ×2，D29）
        - 其它：显式 delta（streak 场景）或 cfg 基础值；纪念日 ×2 取整
        """
        if reason == "gift":
            return (
                self._cfg.gift_anniversary_delta
                if is_anniversary_today()
                else self._cfg.gift_delta
            )
        if delta is not None:
            base = int(delta)
        elif reason == "carry":
            base = self._cfg.carry_delta
        elif reason == "chat":
            base = self._cfg.chat_delta
        elif reason == "feed":
            base = self._cfg.feed_delta
        elif reason == "streak":
            raise ValueError("streak 需显式传入 delta（= streak×2，D30）")
        else:  # pragma: no cover - INTIMACY_REASONS 校验由 award 前置
            raise ValueError(f"未知积分 reason: {reason!r}")
        if is_anniversary_today():
            base *= 2  # 纪念日统一 ×2（除 gift 外，D29）
        return base

    # -- 合入入口 -- #

    def award(
        self,
        reason: str,
        *,
        delta: int | None = None,
        now: float | None = None,
    ) -> int:
        """合入一次积分。返回实际合入 delta（0 = 拒绝）。

        - reason ∈ INTIMACY_REASONS，否则 ValueError
        - 步骤：delta 计算 → carry 门控 → sign → apply_intimacy_event
          （拒绝则返回 0 不发）→ sync.send(PET_FEED, payload) → carry 门控记录更新
        - 负载键集合恰为 {delta, reason, ts, sig}（载荷纯净断言点）
        """
        if reason not in INTIMACY_REASONS:
            raise ValueError(f"reason 必须 ∈ {INTIMACY_REASONS}，得到 {reason!r}")
        ts = int(now) if now is not None else int(time.time())

        # 带话频率门控（D28）：距上次成功带话积分 < carry_min_interval → 拒绝
        if reason == "carry":
            last = _kv_get(self._db, _KV_CARRY_LAST)
            if last is not None and ts - int(last) < self._cfg.carry_min_interval:
                return 0

        amount = self.delta_for(reason, delta=delta)
        ev = {
            "delta": amount,
            "reason": reason,
            "ts": ts,
            "sig": sign_intimacy_event(self._mac_key, amount, reason, ts),
        }
        if not apply_intimacy_event(self._db, ev, self._mac_key):
            return 0  # 超上限 / 验签失败 / 熔断：不发送（防作弊一致性）
        self._sync.send(EventType.PET_FEED, ev)
        if reason == "carry":
            _kv_set(self._db, _KV_CARRY_LAST, str(ts))
        return amount
