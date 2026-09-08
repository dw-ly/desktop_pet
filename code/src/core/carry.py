"""带话发送流程与状态机编排（对应 carry-message impl §4 / plan S2+S4）。

- 常量与 `CarryConfig` 本模块统一定义（impl §1.1）
- `CarryService` 发送侧（propose/confirm_send/revoke/send_ack）与接收侧
  （on_ack/on_revoke，sync 线程 add_handler 回调）编排
- D3 竞态收敛：发送端 revoked 后收 ack → 回滚 delivered（已读优先）
- 定时扫描：start_timer/stop_timer 由调用方驱动，每周期 scan_expired
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from core.carry_intent import detect_carry
from core.carry_store import CarryRecord, CarryStatus, CarryStore
from sync.events import EventType, Message

REVOKE_WINDOW_SECONDS = 120
AUTO_ACK_SECONDS = 180
DEFAULT_EXPIRES_SECONDS = 86400
SCAN_INTERVAL_SECONDS = 30


def _to_minutes(hhmm: str) -> int | None:
    """"22:00" → 当日分钟数 1320；非法格式返回 None。"""
    try:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


@dataclass(frozen=True)
class CarryConfig:
    """带话配置（impl §4.1）。"""

    revoke_window_seconds: int = REVOKE_WINDOW_SECONDS
    auto_ack_seconds: int = AUTO_ACK_SECONDS
    default_expires_seconds: int = DEFAULT_EXPIRES_SECONDS
    scan_interval_seconds: int = SCAN_INTERVAL_SECONDS
    dnd_start: str = ""   # "22:00"；空串 = 不启用勿扰
    dnd_end: str = ""     # "08:00"

    def is_dnd(self, now: int) -> bool:
        """当前时刻是否在勿扰时段。跨午夜区间（start > end）按今日 start → 次日 end 判断。"""
        if not self.dnd_start or not self.dnd_end:
            return False
        start = _to_minutes(self.dnd_start)
        end = _to_minutes(self.dnd_end)
        if start is None or end is None or start == end:
            return False
        lt = time.localtime(now)
        cur = lt.tm_hour * 60 + lt.tm_min
        if start < end:
            return start <= cur < end
        return cur >= start or cur < end  # 跨午夜


class CarryService:
    """带话服务：发送/接收编排。方法会在多线程被调用，store 内部持锁。"""

    def __init__(
        self,
        store: CarryStore,
        sync,
        cfg: CarryConfig,
        *,
        on_confirm: Callable[[CarryRecord], None],
        on_status: Callable[[CarryRecord], None],
        on_hide: Callable[[str], None] | None = None,
        get_mood: Callable[[], str | None] | None = None,
    ) -> None:
        self._store = store
        self._sync = sync
        self._cfg = cfg
        self._on_confirm = on_confirm
        self._on_status = on_status
        self._on_hide = on_hide
        self._get_mood = get_mood or (lambda: None)
        self._timer: threading.Timer | None = None

    @property
    def config(self) -> CarryConfig:
        """只读配置（供 CarryReceiver 等协作模块读取）。"""
        return self._cfg

    # ------------------------------------------------------------------ #
    # 发送侧（UI 线程）
    # ------------------------------------------------------------------ #

    def propose(self, text: str) -> bool:
        """意图检测；命中 → 建 draft 记录 → on_confirm 弹确认框。返回是否进入带话流程。"""
        r = detect_carry(text)
        if not r.is_carry:
            return False
        rec = self._store.create_outgoing(
            r.text, int(time.time()) + self._cfg.default_expires_seconds
        )
        self._on_confirm(rec)
        return True

    def confirm_send(self, carry_id: str) -> None:
        """确认发送：仅 DRAFT 有效；mark_sent → 经 sync 加密外发 msg.carry。"""
        rec = self._store.get(carry_id)
        if rec is None or rec.status != CarryStatus.DRAFT:
            return
        now = int(time.time())
        if not self._store.mark_sent(carry_id, now):
            return
        self._sync.send(
            EventType.MSG_CARRY,
            {
                "carryId": rec.id,
                "text": rec.text,
                "mood": self._get_mood(),
                "expireAt": rec.expires_at,
            },
            expires_at=rec.expires_at,
        )
        self._on_status(self._store.get(carry_id))

    def revoke(self, carry_id: str) -> None:
        """撤回：仅 out + SENT 有效；now - sent_at <= 窗口 才允许。"""
        rec = self._store.get(carry_id)
        if rec is None or rec.direction != "out" or rec.status != CarryStatus.SENT:
            return
        now = int(time.time())
        if rec.sent_at is not None and now - rec.sent_at > self._cfg.revoke_window_seconds:
            return  # 超窗拒绝
        if not self._store.mark_revoked(carry_id, now):
            return
        self._sync.send(
            EventType.CARRY_REVOKE,
            {"carryId": rec.id, "sentAt": rec.sent_at or now},
        )
        self._on_status(self._store.get(carry_id))

    def send_ack(self, carry_id: str, *, auto: bool) -> None:
        """接收端确认：in + SENT 有效；mark_delivered → 回 carry.ack。"""
        rec = self._store.get(carry_id)
        if rec is None or rec.direction != "in" or rec.status != CarryStatus.SENT:
            return
        now = int(time.time())
        if not self._store.mark_delivered(carry_id, now):
            return
        self._sync.send(
            EventType.CARRY_ACK,
            {"carryId": rec.id, "ackedAt": now, "auto": auto},
        )
        self._on_status(self._store.get(carry_id))

    # ------------------------------------------------------------------ #
    # 接收侧（sync 线程 add_handler 回调）
    # ------------------------------------------------------------------ #

    def on_ack(self, msg: Message) -> None:
        """发送端收 ack：sent→delivered；已 revoked 后 ack 后到 → D3 回滚。"""
        rid = msg.payload.get("carryId")
        rec = self._store.get(rid) if rid else None
        if rec is None or rec.direction != "out":
            return
        read_at = msg.payload.get("ackedAt", msg.ts)
        if self._store.mark_delivered(rid, read_at):
            self._on_status(self._store.get(rid))
        elif rec.status == CarryStatus.REVOKED:
            # D3：对方已确认，撤回失败，已读优先回滚
            if self._store.rollback_revoked_to_delivered(rid, read_at):
                self._on_status(self._store.get(rid))

    def on_revoke(self, msg: Message) -> None:
        """接收端收 revoke：未确认则置 revoked 并即时隐藏；已确认则忽略（已读优先）。"""
        rid = msg.payload.get("carryId")
        rec = self._store.get(rid) if rid else None
        if rec is None or rec.direction != "in":
            return
        if rec.read_at is not None:
            return  # 已手动确认，撤回无效（保持 delivered）
        # D2：不校验时间窗（发送方 revoke() 已保证窗口内；迟到 revoke 以 read_at 收敛两端）
        if self._store.mark_revoked(rid, msg.ts):
            if self._on_hide:
                self._on_hide(rid)
            self._on_status(self._store.get(rid))

    # ------------------------------------------------------------------ #
    # 定时扫描
    # ------------------------------------------------------------------ #

    def scan_expired(self, now: int) -> None:
        """发送端过期未送达 → 标记 failed 并通知。"""
        for rec in self._store.pending_expired(now):
            if self._store.mark_failed(rec.id):
                self._on_status(self._store.get(rec.id))

    def start_timer(self) -> None:
        """启动周期扫描定时器（调用方 Core 启动时驱动）。"""
        if self._timer and self._timer.is_alive():
            return
        self._timer = threading.Timer(self._cfg.scan_interval_seconds, self._timer_tick)
        self._timer.daemon = True
        self._timer.start()

    def _timer_tick(self) -> None:
        self.scan_expired(int(time.time()))
        self.start_timer()  # 重新调度下一周期

    def stop_timer(self) -> None:
        """停止定时器（调用方 Core 退出时驱动）。"""
        if self._timer:
            self._timer.cancel()
            self._timer = None
