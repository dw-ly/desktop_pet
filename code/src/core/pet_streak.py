"""共同养成 每日结算与灰色期状态机（对应 pet-growth impl §6 / plan S4）。

每日零点 `settle()`：
- 活动判定（D24）：本端活动 = events 表 status ∈ {sent, pending} 且 type ∈
  ACTIVITY_TYPES；对方活动 = status == 'received'。both_active = 两端均有活动。
- 灰色期状态机（spec §3.3.2 / 04 号笔记）：normal 双活跃 streak+1 并生成方发
  streak 积分；normal 单活跃转 grace（灰色计数 3）；grace 双活跃照常 +1 并累计
  恢复日，连续 3 日恢复 normal；grace 漏登灰色计数 -1 且恢复日清零，耗尽清零重来。
- streak 积分单一生成方（D25）：peer_id 较大一侧生成 `streak×2` 事件（D30 经
  `PetGrowth.award('streak', delta=streak*2)`），配合 INTIMACY_DAILY_LIMITS
  ['streak']=1 兜底防双计。
- 幂等：settle 成功写 `kv['pet:settled:<day>']`，同日重复调用返回当前态。
- 断网推迟：!both_active 且未连接 → DEFERRED（与 daily_align 语义一致），不写
  结算标记、不转变状态，重连后由调用方重新触发。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

from sync.transport import ConnState

from .pet_config import ACTIVITY_TYPES, PetConfig, load_pet_config


class SettleStatus(str, Enum):
    OK = "ok"                # 已结算
    DEFERRED = "deferred"    # 断网且无对方证据，推迟


@dataclass(frozen=True)
class SettleOutcome:
    status: SettleStatus
    streak: int              # 结算后 streak_days
    grace_status: str        # 'normal' | 'grace'
    grace_left: int
    grace_progress: int
    awarded_delta: int       # 本次 streak 积分（0 = 无/非生成方/被拒）
    event_sent: bool         # 本端是否为 streak 事件生成方且已发
    both_active: bool


_KV_GRACE = "pet:grace_status"
_KV_SETTLED = "pet:settled:"


def _kv_get(db, key: str) -> str | None:
    row = db.query_one("SELECT value FROM kv WHERE key=?", (key,))
    return str(row["value"]) if row else None


def _kv_set(db, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _pet_state_opt(db, key: str) -> int | None:
    row = db.query_one("SELECT value FROM pet_state WHERE key=?", (key,))
    return int(row["value"]) if row else None


def _set_pet_state(db, key: str, value: int) -> None:
    db.execute(
        "INSERT INTO pet_state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _day_window(ts: int) -> tuple[int, int, str]:
    """本地日窗口 [day_start, day_end) + 'YYYY-MM-DD' 键（DST 安全用 mktime）。"""
    lt = time.localtime(ts)
    day_start = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    day_key = time.strftime("%Y-%m-%d", lt)
    return day_start, day_start + 86400, day_key


class PetStreak:
    """每日结算 + 灰色期状态机（依赖 events 表活动判定）。"""

    def __init__(self, db, growth, cfg: PetConfig | None = None) -> None:
        self._db = db
        self._growth = growth  # 生成方 streak 积分经 growth.award（D25/D30）
        self._sync = growth._sync  # connected 缺省查询用（鸭子类型）
        self._cfg = cfg or load_pet_config()

    # ------------------------------------------------------------------ #
    # 活动判定（D24）
    # ------------------------------------------------------------------ #

    def _has_activity(self, day_start: int, day_end: int, *, received: bool) -> bool:
        statuses = ("received",) if received else ("sent", "pending")
        sql = (
            "SELECT 1 FROM events WHERE status IN ({}) AND type IN ({}) "
            "AND created_at >= ? AND created_at < ? LIMIT 1"
        ).format(
            ",".join("?" * len(statuses)),
            ",".join("?" * len(ACTIVITY_TYPES)),
        )
        params = statuses + tuple(ACTIVITY_TYPES) + (day_start, day_end)
        return self._db.query_one(sql, params) is not None

    # ------------------------------------------------------------------ #
    # 结算
    # ------------------------------------------------------------------ #

    def settle(
        self,
        my_peer_id: str,
        partner_id: str,
        *,
        now: float | None = None,
        connected: bool | None = None,
    ) -> SettleOutcome:
        """按 §6.1 状态机结算当日。connected 缺省查 sync.connection_status()。"""
        ts = int(now) if now is not None else int(time.time())
        day_start, day_end, day_key = _day_window(ts)

        local_active = self._has_activity(day_start, day_end, received=False)
        peer_active = self._has_activity(day_start, day_end, received=True)
        both_active = local_active and peer_active

        # 幂等：同日已结算 → 返回当前态（不重复 +1）
        if _kv_get(self._db, _KV_SETTLED + day_key) is not None:
            return self._current_outcome(SettleStatus.OK, both_active)

        if not both_active:
            is_connected = (
                self._sync.connection_status() == ConnState.CONNECTED
                if connected is None
                else bool(connected)
            )
            if not is_connected:
                # 断网且无对方证据：推迟（与 daily_align DEFERRED 一致）
                return self._current_outcome(SettleStatus.DEFERRED, both_active)

        # 读取当前状态
        streak = _pet_state_opt(self._db, "streak_days") or 0
        grace_status = _kv_get(self._db, _KV_GRACE) or "normal"
        grace_left = _pet_state_opt(self._db, "grace_left")
        grace_left = grace_left if grace_left is not None else self._cfg.grace_days
        grace_progress = _pet_state_opt(self._db, "grace_progress") or 0

        # 状态机（事务内原子）
        with self._db.transaction():
            if grace_status == "normal":
                if both_active:
                    streak += 1
                else:
                    grace_status = "grace"
                    grace_left = self._cfg.grace_days
                    grace_progress = 0
            else:  # grace
                if both_active:
                    streak += 1
                    grace_progress += 1
                    if grace_progress >= self._cfg.grace_recover_days:
                        grace_status = "normal"
                        grace_left = self._cfg.grace_days
                        grace_progress = 0
                else:
                    grace_left -= 1
                    grace_progress = 0
                    if grace_left <= 0:
                        streak = 0
                        grace_status = "normal"
                        grace_left = self._cfg.grace_days
                        grace_progress = 0

            _set_pet_state(self._db, "streak_days", streak)
            _set_pet_state(self._db, "grace_left", grace_left)
            _set_pet_state(self._db, "grace_progress", grace_progress)
            _kv_set(self._db, _KV_GRACE, grace_status)

        # streak 积分：单一生成方（D25），生成方经 award 发送（D30）
        awarded_delta = 0
        event_sent = False
        if both_active and my_peer_id > partner_id:
            amount = self._growth.award("streak", delta=streak * 2, now=ts)
            if amount > 0:
                awarded_delta = amount
                event_sent = True

        _kv_set(self._db, _KV_SETTLED + day_key, "1")
        return SettleOutcome(
            status=SettleStatus.OK,
            streak=streak,
            grace_status=grace_status,
            grace_left=grace_left,
            grace_progress=grace_progress,
            awarded_delta=awarded_delta,
            event_sent=event_sent,
            both_active=both_active,
        )

    def _current_outcome(
        self, status: SettleStatus, both_active: bool
    ) -> SettleOutcome:
        """读取当前持久化状态构造 outcome（幂等/推迟场景，不修改任何数据）。"""
        streak = _pet_state_opt(self._db, "streak_days") or 0
        grace_status = _kv_get(self._db, _KV_GRACE) or "normal"
        grace_left = _pet_state_opt(self._db, "grace_left")
        grace_left = grace_left if grace_left is not None else self._cfg.grace_days
        grace_progress = _pet_state_opt(self._db, "grace_progress") or 0
        return SettleOutcome(
            status=status,
            streak=streak,
            grace_status=grace_status,
            grace_left=grace_left,
            grace_progress=grace_progress,
            awarded_delta=0,
            event_sent=False,
            both_active=both_active,
        )
