"""农历换算与每日触发扫描测试（anniversary impl §3.3 / plan S1）。"""

from __future__ import annotations

import pytest
from datetime import date

from core.anniv_calendar import AnnivCalendar, lunar_to_solar, occurrence_date
from core.anniv_store import AnnivStore
from core.db import init_core


def _add(db, **patch) -> dict:
    e = {
        "title": "七夕",
        "date": "07-07",
        "calendar": "lunar",
        "repeat": "yearly",
        "notify_days_before": 1,
        "updated_at": 100,
    }
    e.update(patch)
    return AnnivStore(db).add(e)


# --------------------------------------------------------------------------- #
# 农历换算
# --------------------------------------------------------------------------- #

def test_lunar_to_solar_qixi():
    assert lunar_to_solar(2024, 7, 7) == date(2024, 8, 10)


def test_lunar_to_solar_boundary():
    assert lunar_to_solar(1900, 1, 1) == date(1900, 1, 31)
    assert lunar_to_solar(2099, 12, 1) == date(2100, 1, 10)  # 换算落次年属正常


def test_lunar_to_solar_out_of_range():
    with pytest.raises(ValueError):
        lunar_to_solar(1899, 1, 1)
    with pytest.raises(ValueError):
        lunar_to_solar(2100, 1, 1)


def test_lunar_to_solar_invalid_month_day():
    with pytest.raises(ValueError):
        lunar_to_solar(2024, 13, 1)
    with pytest.raises(ValueError):
        lunar_to_solar(2024, 1, 31)  # 农历月最长 30 天


# --------------------------------------------------------------------------- #
# occurrence_date / next_occurrence
# --------------------------------------------------------------------------- #

def test_occurrence_once_year_match():
    e = {"date": "2026-11-11", "calendar": "solar", "repeat": "once"}
    assert occurrence_date(e, 2026) == date(2026, 11, 11)
    assert occurrence_date(e, 2027) is None


def test_occurrence_yearly_lunar():
    e = {"date": "07-07", "calendar": "lunar", "repeat": "yearly"}
    assert occurrence_date(e, 2024) == date(2024, 8, 10)


def test_next_occurrence_once_past(tmp_path):
    db = init_core(tmp_path)
    e = _add(db, repeat="once", date="2026-11-11", calendar="solar")
    cal = AnnivCalendar(db)
    assert cal.next_occurrence(e, date(2026, 12, 1)) is None
    assert cal.next_occurrence(e, date(2026, 11, 11)) == date(2026, 11, 11)
    db.close()


def test_next_occurrence_yearly_cross_year(tmp_path):
    db = init_core(tmp_path)
    e = _add(db, title="元旦", date="01-01", calendar="solar", repeat="yearly")
    cal = AnnivCalendar(db)
    assert cal.next_occurrence(e, date(2026, 12, 30)) == date(2027, 1, 1)  # D37 跨年提醒
    assert cal.next_occurrence(e, date(2026, 1, 1)) == date(2026, 1, 1)
    db.close()


# --------------------------------------------------------------------------- #
# 每日触发扫描
# --------------------------------------------------------------------------- #

def test_daily_triggers_celebrate(tmp_path):
    db = init_core(tmp_path)
    _add(db, title="七夕", date="07-07", calendar="lunar", repeat="yearly", notify_days_before=1)
    cal = AnnivCalendar(db)
    triggers = cal.daily_triggers(date(2024, 8, 10))
    assert len(triggers) == 1
    t = triggers[0]
    assert t.kind == "celebrate"
    assert t.distance == 0
    assert t.occurrence == date(2024, 8, 10)
    db.close()


def test_daily_triggers_remind(tmp_path):
    db = init_core(tmp_path)
    e = _add(db, title="七夕", date="07-07", calendar="lunar", repeat="yearly", notify_days_before=1)
    cal = AnnivCalendar(db)
    triggers = cal.daily_triggers(date(2024, 8, 9))  # 提前 1 天
    assert len(triggers) == 1
    assert triggers[0].kind == "remind"
    assert triggers[0].distance == 1
    assert triggers[0].entry["id"] == e["id"]
    db.close()


def test_daily_triggers_marked_skipped(tmp_path):
    db = init_core(tmp_path)
    e = _add(db, title="七夕", date="07-07", calendar="lunar", repeat="yearly", notify_days_before=1)
    cal = AnnivCalendar(db)
    today = date(2024, 8, 9)
    assert len(cal.daily_triggers(today)) == 1
    cal.mark_triggered(e["id"], "remind", today)
    assert cal.daily_triggers(today) == []  # 标记后跳过
    db.close()


def test_daily_triggers_notify_zero_only_celebrate(tmp_path):
    db = init_core(tmp_path)
    _add(db, title="纪念日", date="02-14", calendar="solar", repeat="yearly", notify_days_before=0)
    cal = AnnivCalendar(db)
    assert cal.daily_triggers(date(2026, 2, 14))[0].kind == "celebrate"
    assert cal.daily_triggers(date(2026, 2, 13)) == []  # notify=0 无 remind
    db.close()


def test_daily_triggers_sorted(tmp_path):
    db = init_core(tmp_path)
    _add(db, title="b", date="02-14", calendar="solar", repeat="yearly", notify_days_before=1)
    _add(db, title="a", date="02-14", calendar="solar", repeat="yearly", notify_days_before=1)
    cal = AnnivCalendar(db)
    ids = [t.entry["id"] for t in cal.daily_triggers(date(2026, 2, 13))]
    assert ids == sorted(ids)
    db.close()


def test_is_anniversary(tmp_path):
    db = init_core(tmp_path)
    _add(db, title="纪念日", date="02-14", calendar="solar", repeat="yearly", notify_days_before=1)
    cal = AnnivCalendar(db)
    assert cal.is_anniversary(date(2026, 2, 14)) is True
    assert cal.is_anniversary(date(2026, 2, 15)) is False
    db.close()
