"""离线队列与去重（对应 data-consistency impl §4.1 / plan S1+S2）。

M2 阶段的独立 SQLite schema（meta / outbox / seen_events）已收编进统一 core.db：
- 发送侧 outbox   → `events` 表（status='pending'→'sent'/'failed'，event_id=f"{本端}:{seq}"）
  在线直发成功也经 `record_sent` 落为 status='sent'（完整双向事件日志，见 D17）
- 接收侧 seen     → `events` 表（status='received'，event_id=f"{对方}:{seq}" 唯一去重）
- 持久化 kv(meta) → `kv` 表（seq 游标等）

db 为 core.db（Database），仅依赖 execute/query_all/query_one/transaction 鸭子类型，
避免 core→sync 循环导入。全部 DB 操作约定只在同步线程内调用（单写者）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Awaitable, Callable

from .events import Message
from .errors import SyncError
from .protocol import SeqManager

log = logging.getLogger(__name__)

# kv 标记键：旧 sync_queue.db 数据迁移完成标记
LEGACY_MIGRATION_KEY = "legacy_queue_migrated"


class SyncQueue:
    """发送出站队列 + 接收去重（收编 core.db.events）。

    构造需传入本端 peer_id（发送侧 event_id 前缀）；db 使用鸭子类型
    （execute / query_all / query_one / transaction），不依赖 core 具体类型。
    """

    def __init__(self, db, my_peer_id: str) -> None:
        self._db = db
        self._my_peer_id = my_peer_id
        # 发送游标以本端为键（event_id 前缀为本端）：序号对本端全局单调，
        # 换机重配对后对方 peer_id 变化不导致序号复位、不与历史 event_id 冲突。
        # 游标缺失时初始化为已存在本端事件最大 seq+1，覆盖换机重配对与旧库迁移
        # 产生的历史事件（防 UNIQUE 撞车）。
        cur_key = f"{my_peer_id}:next_seq"
        if self.meta_get(cur_key) is None:
            row = db.query_one(
                "SELECT MAX(seq) AS m FROM events WHERE event_id LIKE ?",
                (f"{my_peer_id}:%",),
            )
            if row and row["m"] is not None:
                self.meta_set(cur_key, str(int(row["m"]) + 1))

    def close(self) -> None:
        """no-op：连接归 core.db 管理，由 init_core 的持有方负责关闭。"""

    # ------------------------------------------------------------------ #
    # meta（序号游标等持久化 kv）
    # ------------------------------------------------------------------ #

    def meta_get(self, key: str) -> str | None:
        row = self._db.query_one("SELECT value FROM kv WHERE key=?", (key,))
        return str(row["value"]) if row else None

    def meta_set(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def make_seq_manager(self, peer_id: str) -> SeqManager:
        # 键控本端而非对方（见 __init__ 说明）；peer_id 形参保留以兼容调用方。
        return SeqManager(self._my_peer_id, self.meta_get, self.meta_set)

    # ------------------------------------------------------------------ #
    # 发送侧（events 表，event_id=f"{本端}:{seq}"）
    # ------------------------------------------------------------------ #

    def enqueue(
        self,
        peer_id: str,
        seq: int,
        type: str,
        payload: dict,
        expires_at: int | None = None,
    ) -> None:
        self._db.execute(
            "INSERT INTO events(event_id, seq, type, peer, payload_json, created_at, status, expires_at) "
            "VALUES(?, ?, ?, ?, ?, ?, 'pending', ?)",
            (
                f"{self._my_peer_id}:{seq}",
                seq,
                type,
                peer_id,
                json.dumps(payload, ensure_ascii=False),
                int(time.time()),
                expires_at,
            ),
        )

    def record_sent(
        self,
        peer_id: str,
        seq: int,
        type: str,
        payload: dict,
        expires_at: int | None = None,
    ) -> None:
        """在线直发成功后记录已发送事件（events 表，status='sent'）。

        与 `enqueue` 同构，仅 status 不同。记录后 events 表成为**完整双向事件
        日志**（本端已发 + 本端已收）：双端重放同一事件集收敛，daily_align 的
        亲密度兜底核对才成立（data-consistency impl D17 / spec §3.3.6）。
        已发送纯日志行由 S3 prune 按保留期清理（pet.feed 为支撑重放豁免）。
        """
        self._db.execute(
            "INSERT INTO events(event_id, seq, type, peer, payload_json, created_at, status, expires_at) "
            "VALUES(?, ?, ?, ?, ?, ?, 'sent', ?)",
            (
                f"{self._my_peer_id}:{seq}",
                seq,
                type,
                peer_id,
                json.dumps(payload, ensure_ascii=False),
                int(time.time()),
                expires_at,
            ),
        )

    async def flush(
        self,
        peer_id: str,
        send_cb: Callable[[Message], Awaitable[None]],
    ) -> list[Exception]:
        """按 seq 升序补发 pending 项。

        send_cb(message) 由调用方（manager）负责加密并发送，成功即返回；
        抛异常则该项保持 pending，留待下次补发；已过期项标记 failed。
        """
        now = int(time.time())
        rows = self._db.query_all(
            "SELECT id, seq, type, payload_json, expires_at "
            "FROM events WHERE peer=? AND status='pending' ORDER BY seq",
            (peer_id,),
        )
        errors: list[Exception] = []
        for row in rows:
            if row["expires_at"] is not None and int(row["expires_at"]) < now:
                self._db.execute(
                    "UPDATE events SET status='failed' WHERE id=?", (row["id"],)
                )
                continue
            try:
                await send_cb(
                    Message(
                        v=1,
                        type=str(row["type"]),
                        from_id=self._my_peer_id,
                        seq=int(row["seq"]),
                        ts=int(time.time()),
                        payload=json.loads(str(row["payload_json"])),
                    )
                )
            except Exception as exc:  # noqa: BLE001 —— 网络层错误，保持 pending
                log.debug("补发失败 seq=%s: %s", row["seq"], exc)
                errors.append(exc)
                continue
            self._db.execute(
                "UPDATE events SET status='sent' WHERE id=?", (row["id"],)
            )
        return errors

    # ------------------------------------------------------------------ #
    # 接收侧去重（单事务，避免"已记录未投递"竞态）
    # ------------------------------------------------------------------ #

    def receive(self, message: Message, deliver: Callable[[Message], None]) -> bool:
        """去重 + 投递。event_id 已存在返回 False（丢弃）；投递抛异常则回滚。"""
        event_id = f"{message.from_id}:{message.seq}"
        with self._db.transaction():
            cur = self._db.execute(
                "INSERT OR IGNORE INTO events(event_id, seq, type, peer, payload_json, created_at, status) "
                "VALUES(?, ?, ?, ?, ?, ?, 'received')",
                (
                    event_id,
                    message.seq,
                    message.type,
                    message.from_id,
                    json.dumps(message.payload, ensure_ascii=False),
                    int(time.time()),
                ),
            )
            if cur.rowcount == 0:
                return False  # 已存在，丢弃
            deliver(message)
            return True


def migrate_legacy_queue(db, queue_db_path: str, my_peer_id: str) -> int:
    """旧 sync_queue.db（outbox/seen_events/meta）→ core.db（events/kv）。幂等。

    - kv.legacy_queue_migrated 已存在 → 返回 0
    - 旧库文件缺失 → 设标记，返回 0
    - 旧库损坏 → log warning，设标记跳过（不阻塞启动）
    - 只读打开旧库；outbox → events（status 保留原值，event_id=f"{my_peer_id}:{seq}"）
      seen_events → events（type=''，peer=from_peer，status='received'，created_at=received_at）
      meta → kv 原样复制（seq 游标不丢，防序号回退）
    - 返回迁移的事件行数（outbox + seen）
    """
    if not db.table_exists("kv"):
        return 0  # 0002 尚未应用（迁移框架异常），跳过
    if db.query_one("SELECT 1 FROM kv WHERE key=?", (LEGACY_MIGRATION_KEY,)):
        return 0

    path = Path(queue_db_path)
    done = lambda: db.execute(  # noqa: E731
        "INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)",
        (LEGACY_MIGRATION_KEY, str(int(time.time()))),
    )
    if not path.exists():
        done()
        return 0

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        log.warning("旧 sync_queue.db 打开失败，跳过迁移: %s", exc)
        done()
        return 0

    try:
        with db.transaction():
            n = 0
            # meta → kv（seq 游标等，防序号回退）
            for row in conn.execute("SELECT key, value FROM meta"):
                db.execute(
                    "INSERT INTO kv(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (row[0], row[1]),
                )
            # outbox → events（发送侧；event_id 前缀为本端）
            cols_out = [c[1] for c in conn.execute("PRAGMA table_info(outbox)")]
            if cols_out:
                for peer_id, seq, type_, payload_json, expires_at, created_at, status in conn.execute(
                    "SELECT peer_id, seq, type, payload_json, expires_at, created_at, status FROM outbox"
                ):
                    db.execute(
                        "INSERT OR IGNORE INTO events(event_id, seq, type, peer, payload_json, created_at, status, expires_at) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            f"{my_peer_id}:{seq}",
                            seq,
                            type_,
                            peer_id,
                            payload_json,
                            created_at,
                            status or "pending",
                            expires_at,
                        ),
                    )
                    n += 1
            # seen_events → events（接收侧；去重记录无需业务类型，type=''）
            cols_seen = [c[1] for c in conn.execute("PRAGMA table_info(seen_events)")]
            if cols_seen:
                for from_peer, seq, received_at in conn.execute(
                    "SELECT from_peer, seq, received_at FROM seen_events"
                ):
                    db.execute(
                        "INSERT OR IGNORE INTO events(event_id, seq, type, peer, payload_json, created_at, status) "
                        "VALUES(?, ?, '', ?, NULL, ?, 'received')",
                        (f"{from_peer}:{seq}", seq, from_peer, received_at),
                    )
                    n += 1
            done()
            return n
    except sqlite3.Error as exc:
        # 事务内异常会整体回滚（含标记），但迁移失败不应阻塞启动：补设标记跳过
        log.warning("旧 sync_queue.db 数据迁移失败，跳过: %s", exc)
        done()
        return 0
    finally:
        conn.close()
