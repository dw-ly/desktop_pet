"""冲突合并与亲密度重放测试（data-consistency impl §4.3 / plan S4）。"""

from __future__ import annotations

from datetime import datetime

from core.consistency import (
    INTIMACY_DAILY_LIMITS,
    apply_intimacy_event,
    merge_lww,
    sign_intimacy_event,
    verify_intimacy_signature,
)
from core.db import init_core

MAC_KEY = b"k" * 32  # derive_mac_key 产物（测试用任意 32B）


def _ts(y: int, m: int, d: int, hh: int = 12) -> int:
    return int(datetime(y, m, d, hh).timestamp())


def _ev(delta: int, reason: str, ts: int, *, sig: str | None = None) -> dict:
    return {
        "delta": delta,
        "reason": reason,
        "ts": ts,
        "sig": sig if sig is not None else sign_intimacy_event(MAC_KEY, delta, reason, ts),
    }


def _intimacy(db) -> int:
    row = db.query_one("SELECT value FROM pet_state WHERE key='intimacy'")
    return int(row["value"]) if row else 0


# --------------------------------------------------------------------------- #
# 签名
# --------------------------------------------------------------------------- #

def test_sign_verify_roundtrip() -> None:
    sig = sign_intimacy_event(MAC_KEY, 10, "carry", 1786000000)
    assert verify_intimacy_signature(MAC_KEY, 10, "carry", 1786000000, sig)
    assert not verify_intimacy_signature(MAC_KEY, 11, "carry", 1786000000, sig)  # delta 改
    assert not verify_intimacy_signature(MAC_KEY, 10, "chat", 1786000000, sig)   # reason 改
    assert not verify_intimacy_signature(MAC_KEY, 10, "carry", 1786000001, sig)  # ts 改
    assert not verify_intimacy_signature(b"x" * 32, 10, "carry", 1786000000, sig)  # 错密钥


# --------------------------------------------------------------------------- #
# apply_intimacy_event：验签 / 上限 / 熔断 / 累加
# --------------------------------------------------------------------------- #

def test_apply_adds_intimacy(tmp_path) -> None:
    db = init_core(tmp_path)
    assert apply_intimacy_event(db, _ev(10, "carry", _ts(2026, 8, 4)), MAC_KEY)
    assert _intimacy(db) == 10
    assert apply_intimacy_event(db, _ev(5, "chat", _ts(2026, 8, 4)), MAC_KEY)
    assert _intimacy(db) == 15
    db.close()


def test_apply_unknown_reason_rejected(tmp_path) -> None:
    db = init_core(tmp_path)
    assert not apply_intimacy_event(db, _ev(10, "hack", _ts(2026, 8, 4)), MAC_KEY)
    assert _intimacy(db) == 0
    db.close()


def test_apply_nonpositive_delta_rejected(tmp_path) -> None:
    db = init_core(tmp_path)
    assert not apply_intimacy_event(db, _ev(0, "carry", _ts(2026, 8, 4)), MAC_KEY)
    assert not apply_intimacy_event(db, _ev(-5, "carry", _ts(2026, 8, 4)), MAC_KEY)
    assert _intimacy(db) == 0
    db.close()


def test_apply_bad_signature_rejected(tmp_path) -> None:
    db = init_core(tmp_path)
    assert not apply_intimacy_event(db, _ev(10, "carry", _ts(2026, 8, 4), sig="0" * 64), MAC_KEY)
    assert _intimacy(db) == 0
    db.close()


def test_apply_missing_fields_rejected(tmp_path) -> None:
    db = init_core(tmp_path)
    assert not apply_intimacy_event(db, {"delta": 10}, MAC_KEY)
    assert not apply_intimacy_event(db, {"delta": "10", "reason": "carry", "ts": 0, "sig": "x"}, MAC_KEY)
    assert _intimacy(db) == 0
    db.close()


def test_apply_daily_limit_chat(tmp_path) -> None:
    db = init_core(tmp_path)
    day = _ts(2026, 8, 4)
    assert apply_intimacy_event(db, _ev(5, "chat", day), MAC_KEY)
    assert not apply_intimacy_event(db, _ev(5, "chat", day + 60), MAC_KEY)  # 当日第 2 次
    assert _intimacy(db) == 5
    # 次日重置
    assert apply_intimacy_event(db, _ev(5, "chat", _ts(2026, 8, 5)), MAC_KEY)
    assert _intimacy(db) == 10
    db.close()


def test_apply_daily_limit_feed(tmp_path) -> None:
    db = init_core(tmp_path)
    day = _ts(2026, 8, 4)
    for i in range(INTIMACY_DAILY_LIMITS["feed"]):
        assert apply_intimacy_event(db, _ev(3, "feed", day + i * 60), MAC_KEY)
    assert not apply_intimacy_event(db, _ev(3, "feed", day + 600), MAC_KEY)
    assert _intimacy(db) == 5 * 3
    db.close()


def test_apply_limit_counts_by_reason(tmp_path) -> None:
    db = init_core(tmp_path)
    day = _ts(2026, 8, 4)
    assert apply_intimacy_event(db, _ev(5, "chat", day), MAC_KEY)
    assert not apply_intimacy_event(db, _ev(5, "chat", day + 60), MAC_KEY)  # chat 超限
    assert apply_intimacy_event(db, _ev(3, "feed", day + 120), MAC_KEY)     # feed 不受影响
    assert _intimacy(db) == 8
    db.close()


def test_apply_count_scoped_by_event_day(tmp_path) -> None:
    """每日计数按事件 ts 的本地日归属（重放历史事件不计入今天）。"""
    db = init_core(tmp_path)
    assert apply_intimacy_event(db, _ev(5, "chat", _ts(2026, 7, 1)), MAC_KEY)          # 7-1 首次
    assert not apply_intimacy_event(db, _ev(5, "chat", _ts(2026, 7, 1, 23)), MAC_KEY)  # 7-1 第 2 次拒
    assert apply_intimacy_event(db, _ev(5, "chat", _ts(2026, 7, 2)), MAC_KEY)          # 7-2 新日 OK
    assert not apply_intimacy_event(db, _ev(5, "chat", _ts(2026, 7, 2, 8)), MAC_KEY)   # 7-2 第 2 次拒
    assert _intimacy(db) == 10
    db.close()


def test_apply_halt_after_consecutive_failures(tmp_path) -> None:
    db = init_core(tmp_path)
    day = _ts(2026, 8, 4)
    for _ in range(2):
        assert not apply_intimacy_event(db, _ev(10, "carry", day, sig="0" * 64), MAC_KEY)
    assert not apply_intimacy_event(db, _ev(10, "carry", day, sig="0" * 64), MAC_KEY)  # 第 3 次熔断
    # 熔断后：合法事件也拒绝合入
    assert not apply_intimacy_event(db, _ev(10, "carry", day), MAC_KEY)
    assert _intimacy(db) == 0
    db.close()


def test_apply_success_resets_fail_streak(tmp_path) -> None:
    db = init_core(tmp_path)
    day = _ts(2026, 8, 4)
    assert not apply_intimacy_event(db, _ev(10, "carry", day, sig="0" * 64), MAC_KEY)
    assert not apply_intimacy_event(db, _ev(10, "carry", day, sig="0" * 64), MAC_KEY)
    assert apply_intimacy_event(db, _ev(10, "carry", day), MAC_KEY)  # 成功清零
    assert not apply_intimacy_event(db, _ev(10, "carry", day, sig="0" * 64), MAC_KEY)  # 重新计数
    assert not apply_intimacy_event(db, _ev(10, "carry", day, sig="0" * 64), MAC_KEY)
    assert not apply_intimacy_event(db, _ev(10, "carry", day, sig="0" * 64), MAC_KEY)  # 熔断
    assert not apply_intimacy_event(db, _ev(10, "carry", day), MAC_KEY)  # 已熔断
    assert _intimacy(db) == 10
    db.close()


def test_apply_streak_reason_limited_to_one(tmp_path) -> None:
    db = init_core(tmp_path)
    day = _ts(2026, 8, 4)
    assert apply_intimacy_event(db, _ev(20, "streak", day), MAC_KEY)
    assert not apply_intimacy_event(db, _ev(20, "streak", day + 60), MAC_KEY)
    assert _intimacy(db) == 20
    db.close()


# --------------------------------------------------------------------------- #
# merge_lww：LWW 合并
# --------------------------------------------------------------------------- #

def test_merge_insert_new_key(tmp_path) -> None:
    db = init_core(tmp_path)
    ts = _ts(2026, 8, 4)
    assert merge_lww(db, "anniversaries", "id", "updated_at",
                     {"id": "a1", "title": "纪念日", "date": "2026-08-05",
                      "repeat": "yearly", "calendar": "solar",
                      "notify_days_before": 3, "updated_at": ts}, ts)
    row = db.query_one("SELECT * FROM anniversaries WHERE id='a1'")
    assert row["title"] == "纪念日"
    assert row["updated_at"] == ts
    db.close()


def test_merge_newer_ts_updates(tmp_path) -> None:
    db = init_core(tmp_path)
    t1, t2 = _ts(2026, 8, 4), _ts(2026, 8, 5)
    merge_lww(db, "anniversaries", "id", "updated_at",
              {"id": "a1", "title": "旧", "date": "2026-08-05", "repeat": "yearly",
               "calendar": "solar", "notify_days_before": 3, "updated_at": t1}, t1)
    assert merge_lww(db, "anniversaries", "id", "updated_at",
                     {"id": "a1", "title": "新", "date": "2026-08-06", "repeat": "yearly",
                      "calendar": "solar", "notify_days_before": 7, "updated_at": t2}, t2)
    row = db.query_one("SELECT * FROM anniversaries WHERE id='a1'")
    assert row["title"] == "新"
    assert row["date"] == "2026-08-06"
    db.close()


def test_merge_older_or_equal_ts_ignored(tmp_path) -> None:
    db = init_core(tmp_path)
    t1, t2 = _ts(2026, 8, 4), _ts(2026, 8, 5)
    merge_lww(db, "anniversaries", "id", "updated_at",
              {"id": "a1", "title": "旧", "date": "2026-08-05", "repeat": "yearly",
               "calendar": "solar", "notify_days_before": 3, "updated_at": t2}, t2)
    assert not merge_lww(db, "anniversaries", "id", "updated_at",
                         {"id": "a1", "title": "更旧", "date": "2026-08-04", "repeat": "yearly",
                          "calendar": "solar", "notify_days_before": 1, "updated_at": t1}, t1)
    assert not merge_lww(db, "anniversaries", "id", "updated_at",
                         {"id": "a1", "title": "同值", "date": "2026-08-05", "repeat": "yearly",
                          "calendar": "solar", "notify_days_before": 3, "updated_at": t2}, t2)
    assert db.query_one("SELECT title FROM anniversaries WHERE id='a1'")["title"] == "旧"
    db.close()


def test_merge_distinct_keys_kept(tmp_path) -> None:
    db = init_core(tmp_path)
    t = _ts(2026, 8, 4)
    for i in range(3):
        assert merge_lww(db, "anniversaries", "id", "updated_at",
                         {"id": f"a{i}", "title": f"x{i}", "date": "2026-08-05",
                          "repeat": "yearly", "calendar": "solar",
                          "notify_days_before": 3, "updated_at": t}, t)
    assert db.query_one("SELECT COUNT(*) AS n FROM anniversaries")["n"] == 3
    db.close()


def test_merge_once_ignores_existing(tmp_path) -> None:
    """一次性事件（如装扮解锁 gift.accept）：存在即不覆盖。"""
    db = init_core(tmp_path)
    t1, t2 = _ts(2026, 8, 4), _ts(2026, 8, 5)
    assert merge_lww(db, "user_items", "item_id", None,
                     {"item_id": "i1", "name": "装扮", "type": "costume",
                      "rarity": "common", "source": "B", "expire_at": None}, t1,
                     once=True)
    assert not merge_lww(db, "user_items", "item_id", None,
                         {"item_id": "i1", "name": "重复", "type": "costume",
                          "rarity": "common", "source": "B", "expire_at": None}, t2,
                         once=True)
    assert db.query_one("SELECT name FROM user_items WHERE item_id='i1'")["name"] == "装扮"
    db.close()


def test_merge_no_ts_col_existing_ignored(tmp_path) -> None:
    """无时间戳列表（pet_state）：键已存在则忽略不覆盖。"""
    db = init_core(tmp_path)
    db.execute("INSERT INTO pet_state(key, value) VALUES('exp', 100)")
    assert not merge_lww(db, "pet_state", "key", None, {"key": "exp", "value": 200}, _ts(2026, 8, 4))
    assert db.query_one("SELECT value FROM pet_state WHERE key='exp'")["value"] == 100
    db.close()


def test_merge_missing_key_raises(tmp_path) -> None:
    db = init_core(tmp_path)
    try:
        merge_lww(db, "anniversaries", "id", "updated_at", {"title": "无键"}, _ts(2026, 8, 4))
    except ValueError:
        pass
    else:
        raise AssertionError("缺少键列应抛 ValueError")
    db.close()
