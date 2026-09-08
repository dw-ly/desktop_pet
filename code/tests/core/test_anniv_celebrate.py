"""当天庆祝与限时装扮测试（anniversary impl §5.1 / plan S3）。"""

from __future__ import annotations

import pytest
from datetime import date

from core.anniv_calendar import AnnivCalendar
from core.anniv_celebrate import AnnivCelebrate, set_gift_invite_hook
from core.anniv_config import load_anniv_config
from core.anniv_store import AnnivStore
from core.db import init_core
from sync.events import EventType, Message

TODAY = date(2026, 2, 14)  # 纪念日当天


class FakeSync:
    def __init__(self, paired: bool = True) -> None:
        self.sent: list[tuple[EventType, dict]] = []
        self._paired = paired

    def send(self, type_, payload: dict, **kwargs) -> None:
        self.sent.append((type_, payload))

    def pairing_status(self) -> str:
        return "paired" if self._paired else "unpaired"


@pytest.fixture(autouse=True)
def _reset_gift_hook():
    set_gift_invite_hook(None)
    yield


def _setup(tmp_path, *, paired: bool = True):
    db = init_core(tmp_path)
    sync = FakeSync(paired=paired)
    store = AnnivStore(db)
    cal = AnnivCalendar(db)
    celebrate = AnnivCelebrate(db, sync, cal)
    entry = store.add({
        "title": "在一起纪念日",
        "date": "02-14",
        "calendar": "solar",
        "repeat": "yearly",
        "notify_days_before": 1,
    })
    return db, sync, celebrate, entry


def test_daily_celebrate(tmp_path):
    db, sync, celebrate, entry = _setup(tmp_path)
    actions = celebrate.daily_check(TODAY)
    assert actions == [f"celebrate:{entry['id']}"]
    assert celebrate.current_outfit() == "anniv_limited_suit"
    assert len(sync.sent) == 1
    type_, payload = sync.sent[0]
    assert type_ == EventType.DATE_REMIND
    assert set(payload.keys()) == {"id", "title", "date", "calendar"}
    assert payload["id"] == entry["id"]
    db.close()


def test_daily_remind_preview(tmp_path):
    db, sync, celebrate, entry = _setup(tmp_path)
    reminded = []

    def on_remind(e, text):
        reminded.append((e, text))

    celebrate._on_remind = on_remind
    actions = celebrate.daily_check(date(2026, 2, 13))  # 提前 1 天
    assert actions == [f"remind:{entry['id']}"]
    assert len(reminded) == 1
    assert reminded[0][0]["id"] == entry["id"]
    assert reminded[0][1]  # 非空祝福文案
    assert sync.sent == []  # 预告不发送 date.remind
    db.close()


def test_daily_once_per_day(tmp_path):
    db, sync, celebrate, entry = _setup(tmp_path)
    celebrate.daily_check(TODAY)
    assert celebrate.daily_check(TODAY) == []  # 标记生效
    assert len(sync.sent) == 1
    db.close()


def test_handle_date_remind(tmp_path):
    db, sync, celebrate, _entry = _setup(tmp_path)
    celebrated = []

    def on_celebrate(e):
        celebrated.append(e)

    celebrate._on_celebrate = on_celebrate
    m = Message(v=1, type="date.remind", from_id="peer", seq=7,
                ts=1786000000,
                payload={"id": "p1", "title": "在一起纪念日", "date": "02-14", "calendar": "solar"})
    assert celebrate.handle_date_remind(m) is True
    assert len(celebrated) == 1
    assert celebrated[0]["id"] == "p1"
    assert celebrate.current_outfit() == "anniv_limited_suit"
    assert sync.sent == []  # 不反向发送
    db.close()


def test_handle_date_remind_invalid(tmp_path):
    db, sync, celebrate, _entry = _setup(tmp_path)
    m = Message(v=1, type="date.remind", from_id="peer", seq=8,
                ts=1786000000, payload={})
    assert celebrate.handle_date_remind(m) is False
    assert celebrate.current_outfit() is None
    assert sync.sent == []
    db.close()


def test_outfit_expiry(tmp_path):
    db, sync, celebrate, _entry = _setup(tmp_path)
    celebrate.daily_check(TODAY)
    assert celebrate.check_outfit_expiry(TODAY) is None       # 当天未过期
    assert celebrate.current_outfit() == "anniv_limited_suit"
    assert celebrate.check_outfit_expiry(date(2026, 2, 15)) == "anniv_limited_suit"  # 1 天后失效
    assert celebrate.current_outfit() is None                 # 回归原始装扮
    db.close()


def test_outfit_expiry_duration_config(tmp_path):
    db = init_core(tmp_path)
    sync = FakeSync()
    cal = AnnivCalendar(db)
    cfg = load_anniv_config({"outfit_duration_days": 3})
    celebrate = AnnivCelebrate(db, sync, cal, cfg)
    entry = AnnivStore(db).add({
        "title": "在一起纪念日", "date": "02-14", "calendar": "solar",
        "repeat": "yearly", "notify_days_before": 1,
    })
    celebrate.daily_check(TODAY)
    assert celebrate.check_outfit_expiry(TODAY) is None
    assert celebrate.check_outfit_expiry(date(2026, 2, 16)) is None  # +2 天仍有效
    assert celebrate.check_outfit_expiry(date(2026, 2, 17)) == "anniv_limited_suit"  # +3 天失效
    db.close()


def test_gift_invite_hook(tmp_path):
    db, sync, celebrate, entry = _setup(tmp_path)
    gifts = []
    set_gift_invite_hook(lambda e: gifts.append(e))
    celebrate.daily_check(TODAY)
    assert len(gifts) == 1
    assert gifts[0]["id"] == entry["id"]
    db.close()


def test_unpaired_no_remind(tmp_path):
    db, sync, celebrate, _entry = _setup(tmp_path, paired=False)
    celebrate.daily_check(TODAY)
    assert sync.sent == []                     # 未配对不外发
    assert celebrate.current_outfit() == "anniv_limited_suit"  # 本地庆祝照常
    db.close()
