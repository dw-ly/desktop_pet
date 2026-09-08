"""带话接收编排（对应 carry-message impl §5 / plan S3）。

- `on_msg`：收 msg.carry → 幂等入库 → 勿扰判断 → notify.show_carry 播报（仅一次）
- `scan_auto_ack`：3 分钟未手动确认的 incoming → service.send_ack(auto=True)（由 service 定时器驱动）
- `notify` 为 UI 播报接口（Core 接线时注入）；本模块仅依赖鸭子类型
- `on_ack_sent` 预留（UI 侧"自动确认已发"通知，当前未调用）
"""

from __future__ import annotations

import time
from typing import Callable

from core.carry import CarryService
from core.carry_store import CarryRecord, CarryStore
from sync.events import Message


class CarryReceiver:
    """接收 msg.carry 事件 → 播报；自动 ack 兜底扫描。"""

    def __init__(
        self,
        store: CarryStore,
        service: CarryService,
        notify,
        *,
        is_dnd: Callable[[], bool],
        on_ack_sent: Callable[[CarryRecord], None] | None = None,
    ) -> None:
        self._store = store
        self._service = service
        self._notify = notify
        self._is_dnd = is_dnd
        self._on_ack_sent = on_ack_sent

    def on_msg(self, msg: Message) -> None:
        """收到 msg.carry：幂等入库；非勿扰且首次收到 → 播报。"""
        payload = msg.payload
        rid = payload.get("carryId")
        text = payload.get("text", "")
        exp = payload.get("expireAt")
        if not rid or exp is None:
            return
        rec = self._store.insert_incoming(rid, text, exp)
        if rec is None:
            return  # 重复投递，仅播报一次
        if self._is_dnd():
            return  # 勿扰：静默入库（"稍后看"由 UI 查 store 呈现）
        self._notify.show_carry(
            rec, on_ack=lambda: self._service.send_ack(rid, auto=False)
        )

    def scan_auto_ack(self) -> None:
        """超 auto_ack_seconds 未确认的 incoming → 自动 ack 兜底。"""
        cfg = self._service.config
        cutoff = int(time.time()) - cfg.auto_ack_seconds
        for rec in self._store.unacked_incoming(cutoff):
            self._service.send_ack(rec.id, auto=True)
