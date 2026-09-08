"""每日兜底对齐（对应 data-consistency impl §4.4 / plan S5）。

- `daily_align(sync, db, mac_key)`：每日零点触发。未连接 → DEFERRED（推迟到
  重连后由调用方再触发）；已连接 → 发 `date.sync` 快照（streak/intimacy/ts）
  + 亲密度本地事件流重放兜底核对（与 `pet_state.intimacy` 比对，不一致修复）。
- `handle_peer_snapshot(db, payload)`：收到对端 `date.sync` 快照 → streak 取
  较大者修复（只增兜底；灰色期降级由 pet-growth 状态机管理，不在此覆盖）。
- 亲密度兜底以**本端事件流重放**为准，不直接采用对端数字（spec §3.3.6：
  两端各自从事件流重放收敛）；对端快照仅用于 streak 校准。
- 熔断状态（kv.intimacy_halted，S4 签名连续失败触发）→ MANUAL_NEEDED，
  不做自动修复，提示手动同步（spec §3.4）。
- 本模块为同步纯函数，由后台定时任务调用，不阻塞主线程。
"""

from __future__ import annotations

import json
import time
from enum import Enum

from sync.events import EventType
from sync.transport import ConnState

from .consistency import INTIMACY_DAILY_LIMITS, verify_intimacy_signature
from .db import Database

_KEY_STREAK = "streak_days"
_KV_HALT = "intimacy_halted"


class AlignResult(str, Enum):
    """对齐结果。DEFERRED 为未连接推迟（plan S5 断网推迟语义）。"""

    OK = "ok"
    REPAIRED = "repaired"
    MANUAL_NEEDED = "manual_needed"
    DEFERRED = "deferred"


# --------------------------------------------------------------------------- #
# pet_state 读写
# --------------------------------------------------------------------------- #

def _get_pet_state(db: Database, key: str) -> int:
    row = db.query_one("SELECT value FROM pet_state WHERE key=?", (key,))
    return int(row["value"]) if row else 0


def _set_pet_state(db: Database, key: str, value: int) -> None:
    db.execute(
        "INSERT INTO pet_state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _is_halted(db: Database) -> bool:
    row = db.query_one("SELECT value FROM kv WHERE key=?", (_KV_HALT,))
    return row is not None and row["value"] == "1"


# --------------------------------------------------------------------------- #
# 亲密度重放核对（本端事件流，纯计算不写库）
# --------------------------------------------------------------------------- #

def replay_intimacy_total(db: Database, mac_key: bytes) -> int:
    """重放 events 表 `pet.feed` 事件流，返回收敛总和。

    与 `apply_intimacy_event` 同构：验签 + 每日 reason 上限（按事件 ts 本地日），
    跳过非法事件；**不写库**，仅用于核对 `pet_state.intimacy`。
    """
    counts: dict[tuple[str, str], int] = {}
    total = 0
    rows = db.query_all(
        "SELECT payload_json FROM events WHERE type=? ORDER BY created_at, id",
        (EventType.PET_FEED.value,),
    )
    for row in rows:
        try:
            ev = json.loads(row["payload_json"])
        except ValueError:
            continue
        delta = ev.get("delta")
        reason = ev.get("reason")
        ts = ev.get("ts")
        sig = ev.get("sig")
        if (
            not isinstance(delta, int)
            or not isinstance(reason, str)
            or not isinstance(ts, int)
            or not isinstance(sig, str)
            or reason not in INTIMACY_DAILY_LIMITS
            or delta <= 0
        ):
            continue
        if not verify_intimacy_signature(mac_key, delta, reason, ts, sig):
            continue
        limit = INTIMACY_DAILY_LIMITS.get(reason, 0)
        if limit > 0:
            day = time.strftime("%Y-%m-%d", time.localtime(ts))
            ckey = (day, reason)
            cnt = counts.get(ckey, 0)
            if cnt >= limit:
                continue
            counts[ckey] = cnt + 1
        total += delta
    return total


# --------------------------------------------------------------------------- #
# 对齐入口
# --------------------------------------------------------------------------- #

def build_snapshot(db: Database) -> dict:
    """本端累计数据快照（date.sync payload）。"""
    return {
        "streak": _get_pet_state(db, _KEY_STREAK),
        "intimacy": _get_pet_state(db, "intimacy"),
        "ts": int(time.time()),
    }


def daily_align(
    sync, db: Database, mac_key: bytes | None = None
) -> AlignResult:
    """每日零点兜底对齐。

    `sync`：SyncManager 鸭子类型（connection_status/send）。未连接 → DEFERRED
    （推迟，重连后由调用方再触发）；`mac_key` 为空则跳过亲密度重放（仅发快照，
    供未派生会话密钥的场景，如仅配对未建立会话）。
    """
    if _is_halted(db):
        return AlignResult.MANUAL_NEEDED
    if sync.connection_status() != ConnState.CONNECTED:
        return AlignResult.DEFERRED
    sync.send(EventType.DATE_SYNC, build_snapshot(db))
    if mac_key is None:
        return AlignResult.OK
    total = replay_intimacy_total(db, mac_key)
    if total == _get_pet_state(db, "intimacy"):
        return AlignResult.OK
    _set_pet_state(db, "intimacy", total)
    return AlignResult.REPAIRED


def handle_peer_snapshot(db: Database, payload: dict) -> AlignResult:
    """处理对端 `date.sync` 快照：streak 取较大者修复（只增兜底）。"""
    peer_streak = payload.get("streak")
    if not isinstance(peer_streak, int) or peer_streak <= _get_pet_state(db, _KEY_STREAK):
        return AlignResult.OK
    _set_pet_state(db, _KEY_STREAK, peer_streak)
    return AlignResult.REPAIRED
