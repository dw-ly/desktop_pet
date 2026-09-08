"""带话存储层（对应 carry-message impl §3 / plan G1）。

- `carries` 表由 data-consistency 统一库 `core.db` 基线表
  （`migrations/0001_init.sql`）建表，本模块**不建表、不建库**。
- `CarryStore` 基于统一 `Database` 实现；所有访问经 Database 内部锁串行化。
- 状态机：draft→sent→delivered / revoked / failed；非法流转返回 False（幂等友好，不抛）。
- D3 竞态：发送端已 revoked 后 ack 后到 → `rollback_revoked_to_delivered`（已读优先）。
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from enum import Enum

from core.db import Database


class CarryError(Exception):
    """带话业务基类（本模块内）。"""


class InvalidStateTransition(CarryError):
    """非法状态流转（store 层按约定以返回 False 表达，通常不抛）。"""


class CarryStatus(str, Enum):
    DRAFT = "draft"
    SENT = "sent"
    DELIVERED = "delivered"
    REVOKED = "revoked"
    FAILED = "failed"


@dataclass
class CarryRecord:
    """一条带话记录（与 carries 表逐字段对应）。"""

    id: str
    direction: str                 # 'out' 发送方记录 / 'in' 接收方记录
    text: str
    status: CarryStatus
    expires_at: int
    created_at: int
    sent_at: int | None = None     # out: 发送方发送时间（撤回窗口基准）
    read_at: int | None = None     # 送达确认时间（ack）
    revoked_at: int | None = None  # 撤回时间


class CarryStore:
    """carries 表读写封装 + 状态流转校验。基于统一 core.db。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------ #
    # 行转换
    # ------------------------------------------------------------------ #

    @staticmethod
    def _from_row(row) -> CarryRecord:
        return CarryRecord(
            id=row["id"],
            direction=row["direction"],
            text=row["text"],
            status=CarryStatus(row["status"]),
            expires_at=row["expires_at"],
            created_at=row["created_at"],
            sent_at=row["sent_at"],
            read_at=row["read_at"],
            revoked_at=row["revoked_at"],
        )

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #

    def create_outgoing(self, text: str, expires_at: int) -> CarryRecord:
        """新建发送记录：status=DRAFT、direction='out'、自动分配 uuid id。"""
        carry_id = uuid.uuid4().hex
        now = int(time.time())
        self._db.execute(
            "INSERT INTO carries (id, direction, text, status, expires_at, created_at) "
            "VALUES (?, 'out', ?, 'draft', ?, ?)",
            (carry_id, text, expires_at, now),
        )
        rec = self.get(carry_id)
        assert rec is not None  # 刚插入必然存在
        return rec

    def insert_incoming(
        self, carry_id: str, text: str, expires_at: int
    ) -> CarryRecord | None:
        """新建接收记录：status=SENT、direction='in'；carry_id 已存在（重复投递）→ None（幂等）。"""
        now = int(time.time())
        try:
            self._db.execute(
                "INSERT INTO carries (id, direction, text, status, expires_at, created_at) "
                "VALUES (?, 'in', ?, 'sent', ?, ?)",
                (carry_id, text, expires_at, now),
            )
        except sqlite3.IntegrityError:
            return None
        return self.get(carry_id)

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #

    def get(self, carry_id: str) -> CarryRecord | None:
        row = self._db.query_one("SELECT * FROM carries WHERE id=?", (carry_id,))
        return self._from_row(row) if row else None

    def unacked_incoming(self, before_ts: int) -> list[CarryRecord]:
        """接收端未确认记录：in + sent 且 created_at <= before_ts（自动 ack 扫描）。"""
        rows = self._db.query_all(
            "SELECT * FROM carries WHERE direction='in' AND status='sent' "
            "AND created_at <= ? ORDER BY created_at",
            (before_ts,),
        )
        return [self._from_row(r) for r in rows]

    def pending_expired(self, now: int) -> list[CarryRecord]:
        """发送端过期未送达：out + sent 且 expires_at < now（严格小于，边界不触发）。"""
        rows = self._db.query_all(
            "SELECT * FROM carries WHERE direction='out' AND status='sent' "
            "AND expires_at < ? ORDER BY expires_at",
            (now,),
        )
        return [self._from_row(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 状态流转（非法返回 False，不抛）
    # ------------------------------------------------------------------ #

    def mark_sent(self, carry_id: str, sent_at: int) -> bool:
        """draft → sent（发送方确认发送）。"""
        return self._set_status(
            carry_id, (CarryStatus.DRAFT,), CarryStatus.SENT, sent_at=sent_at
        )

    def mark_delivered(self, carry_id: str, read_at: int) -> bool:
        """sent → delivered（接收端 ack 到达）。"""
        return self._set_status(
            carry_id, (CarryStatus.SENT,), CarryStatus.DELIVERED, read_at=read_at
        )

    def mark_revoked(self, carry_id: str, revoked_at: int) -> bool:
        """sent → revoked（本端撤回 / 接收端收 revoke）。"""
        return self._set_status(
            carry_id, (CarryStatus.SENT,), CarryStatus.REVOKED, revoked_at=revoked_at
        )

    def mark_failed(self, carry_id: str) -> bool:
        """sent → failed（过期扫描）。"""
        return self._set_status(
            carry_id, (CarryStatus.SENT,), CarryStatus.FAILED
        )

    def rollback_revoked_to_delivered(self, carry_id: str, read_at: int) -> bool:
        """D3 竞态回滚：revoked → delivered（撤回后 ack 后到，已读优先）。"""
        return self._set_status(
            carry_id, (CarryStatus.REVOKED,), CarryStatus.DELIVERED, read_at=read_at
        )

    # ------------------------------------------------------------------ #
    # 内部：统一状态流转（并发安全）
    # ------------------------------------------------------------------ #

    def _set_status(
        self,
        carry_id: str,
        from_statuses: tuple[CarryStatus, ...],
        to: CarryStatus,
        **ts: int,
    ) -> bool:
        """UPDATE ... SET status=?, <ts> WHERE id=? AND status IN (...)。

        - 通过 WHERE status IN (...) 原子过滤合法前置状态：rowcount==1 表示流转成功
        - ts 键须为真实列名（sent_at / read_at / revoked_at），由调用方保证
        - 全程经 Database 锁串行化
        """
        set_parts = ["status=?"]
        params: list = [to.value]
        for col, val in ts.items():
            set_parts.append(f"{col}=?")
            params.append(val)
        qmarks = ",".join("?" for _ in from_statuses)
        sql = (
            f"UPDATE carries SET {', '.join(set_parts)} "
            f"WHERE id=? AND status IN ({qmarks})"
        )
        params.extend([carry_id, *(s.value for s in from_statuses)])
        cur = self._db.execute(sql, tuple(params))
        return cur.rowcount == 1
