"""冲突合并与亲密度重放（对应 data-consistency impl §4.3 / plan S4）。

- `merge_lww`：通用 LWW 合并——同一键取最新 ts、不同键各自保留、
  一次性事件（once=True，如 gift.accept 装扮解锁）存在即不覆盖。
- `apply_intimacy_event`：亲密度积分事件合入**唯一入口**（pet-growth 复用）：
  熔断检查 → HMAC 验签（会话密钥派生 MAC 键）→ 每日 reason 上限 → 累加
  `pet_state.intimacy`。防作弊双重约束：签名 + 每日上限（spec §3.3.7）。
- `sign_intimacy_event` / `verify_intimacy_signature`：签名规范化，发送侧与
  本函数同源保证两端互验；签名覆盖 delta/reason/ts（防篡改这三字段）。

重大异常熔断：连续签名失败达阈值 → 置 `kv.intimacy_halted`，停止自动合入
（spec §3.4"签名连续失败停止自动合入并提示手动同步"）；签名成功清零计数。
"""

from __future__ import annotations

import hashlib
import hmac
import time

# 积分 reason 枚举（与 pet-growth 积分来源表一致）
INTIMACY_REASONS = ("carry", "chat", "feed", "gift", "streak")

# 每日 reason 上限（**次数**）。来源表明确的上限：chat=1、feed=5、streak=自然日 1 次；
# carry（每带话 5min 频率限制）与 gift（接受事件为准、发送侧已限礼物次数）
# 由发送侧约束，本侧不设每日上限（0 = 无上限），仅验签防伪。
INTIMACY_DAILY_LIMITS: dict[str, int] = {
    "carry": 0,
    "chat": 1,
    "feed": 5,
    "gift": 0,
    "streak": 1,
}

# 熔断：连续签名失败阈值
INTIMACY_FAIL_HALT_THRESHOLD = 3

_KV_HALT = "intimacy_halted"
_KV_FAIL_STREAK = "intimacy_fail_streak"
_KV_COUNT_PREFIX = "intimacy_count"


# --------------------------------------------------------------------------- #
# 签名（两端同源；pet-growth 发送侧复用 sign_intimacy_event）
# --------------------------------------------------------------------------- #

def _canonical(delta: int, reason: str, ts: int) -> bytes:
    return f"{int(delta)}:{reason}:{int(ts)}".encode("ascii")


def sign_intimacy_event(mac_key: bytes, delta: int, reason: str, ts: int) -> str:
    """HMAC-SHA256(mac_key, f"{delta}:{reason}:{ts}")，hex 输出。"""
    return hmac.new(mac_key, _canonical(delta, reason, ts), hashlib.sha256).hexdigest()


def verify_intimacy_signature(
    mac_key: bytes, delta: int, reason: str, ts: int, sig: str
) -> bool:
    """恒定时间比较；sig 非 hex/参数非法一律 False。"""
    try:
        expect = sign_intimacy_event(mac_key, delta, reason, ts)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expect, sig)


# --------------------------------------------------------------------------- #
# kv 辅助（每日计数 / 熔断状态持久化到 kv 表）
# --------------------------------------------------------------------------- #

def _kv_get(db, key: str) -> str | None:
    row = db.query_one("SELECT value FROM kv WHERE key=?", (key,))
    return row["value"] if row else None


def _kv_set(db, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _bump_fail_streak(db) -> None:
    cur = int(_kv_get(db, _KV_FAIL_STREAK) or "0") + 1
    if cur >= INTIMACY_FAIL_HALT_THRESHOLD:
        _kv_set(db, _KV_HALT, "1")
        _kv_set(db, _KV_FAIL_STREAK, "0")
    else:
        _kv_set(db, _KV_FAIL_STREAK, str(cur))


def _clear_fail_streak(db) -> None:
    _kv_set(db, _KV_FAIL_STREAK, "0")


# --------------------------------------------------------------------------- #
# 亲密度合入（唯一入口）
# --------------------------------------------------------------------------- #

def apply_intimacy_event(db, ev: dict, mac_key: bytes) -> bool:
    """合入一条亲密度积分事件。返回 True=已合入；False=拒绝。

    `ev`：pet.feed 事件的 payload dict——`{"delta": int, "reason": str,
    "ts": int, "sig": hex str}`。签名由调用方用 `derive_mac_key(my_private,
    peer_public)` 派生的 MAC 键校验。

    流程（单事务原子）：熔断检查 → 验签 → 每日 reason 上限 → 累加
    `pet_state.intimacy`（不存在则建行）。
    """
    delta = ev.get("delta")
    reason = ev.get("reason")
    ts = ev.get("ts")
    sig = ev.get("sig")
    if (
        not isinstance(delta, int)
        or not isinstance(reason, str)
        or not isinstance(ts, int)
        or not isinstance(sig, str)
        or reason not in INTIMACY_REASONS
        or delta <= 0
    ):
        return False

    with db.transaction():
        if _kv_get(db, _KV_HALT) == "1":
            return False
        if not verify_intimacy_signature(mac_key, delta, reason, ts, sig):
            _bump_fail_streak(db)
            return False
        _clear_fail_streak(db)

        limit = INTIMACY_DAILY_LIMITS.get(reason, 0)
        if limit > 0:
            day = time.strftime("%Y-%m-%d", time.localtime(ts))
            count_key = f"{_KV_COUNT_PREFIX}:{day}:{reason}"
            cnt = int(_kv_get(db, count_key) or "0")
            if cnt >= limit:
                return False
            _kv_set(db, count_key, str(cnt + 1))

        row = db.query_one("SELECT value FROM pet_state WHERE key='intimacy'")
        cur = int(row["value"]) if row else 0
        if row:
            db.execute(
                "UPDATE pet_state SET value=? WHERE key='intimacy'", (cur + delta,)
            )
        else:
            db.execute(
                "INSERT INTO pet_state(key, value) VALUES('intimacy', ?)",
                (cur + delta,),
            )
        return True


# --------------------------------------------------------------------------- #
# LWW 冲突合并
# --------------------------------------------------------------------------- #

def merge_lww(
    db,
    table: str,
    key_col: str,
    ts_col: str | None,
    row: dict,
    ts: int,
    *,
    once: bool = False,
) -> bool:
    """通用 LWW 合并一行到目标表。返回 True=写入；False=忽略。

    - 键不存在 → 插入（row 须含 ts_col，其值为 `ts`）
    - 键已存在：
      - `once=True`（一次性事件，如装扮解锁 gift.accept）→ 忽略不覆盖
      - 现有 ts >= 新 ts → 忽略（旧值/同值不覆盖）
      - 现有 ts < 新 ts → 更新所有 row 列
    - `ts_col=None` 且键已存在 → 忽略（无时间戳不可比较覆盖）

    `table`/`key_col`/`ts_col` 必须为**代码内受控标识符**（禁止用户输入拼接）。
    """
    if key_col not in row:
        raise ValueError(f"row 缺少键列 {key_col!r}")
    key = row[key_col]

    existing = db.query_one(f"SELECT * FROM {table} WHERE {key_col}=?", (key,))
    if existing is None:
        cols = list(row)
        db.execute(
            f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
            tuple(row[c] for c in cols),
        )
        return True
    if once:
        return False
    if ts_col is None:
        return False
    if int(existing[ts_col] or 0) >= ts:
        return False
    sets = ",".join(f"{c}=?" for c in row)
    db.execute(
        f"UPDATE {table} SET {sets} WHERE {key_col}=?",
        tuple(row[c] for c in row) + (key,),
    )
    return True
