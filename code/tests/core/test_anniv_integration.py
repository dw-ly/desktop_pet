"""养成联动钩子测试（anniversary impl §7.1 / plan S5）。"""

from __future__ import annotations

import pytest
from datetime import date

from core.anniv_calendar import AnnivCalendar
from core.anniv_integration import register_anniversary_hook, unregister_anniversary_hook
from core.anniv_store import AnnivStore
from core.db import init_core
from core.pet_growth import PetGrowth, is_anniversary_today
from sync.events import EventType

MAC_KEY = b"k" * 32
DAY_TS = 1786000000


class FakeSync:
    def __init__(self) -> None:
        self.calls: list[tuple[EventType, dict]] = []

    def send(self, type_, payload: dict, **kwargs) -> None:
        self.calls.append((type_, payload))


@pytest.fixture(autouse=True)
def _reset_hook():
    yield
    unregister_anniversary_hook()  # 避免污染 pet-growth 全局钩子


def _today_entry(store: AnnivStore) -> dict:
    today = date.today()
    return store.add({
        "title": "在一起纪念日",
        "date": today.isoformat(),
        "calendar": "solar",
        "repeat": "once",
        "notify_days_before": 1,
    })


def test_register_hook_true(tmp_path):
    db = init_core(tmp_path)
    store = AnnivStore(db)
    _today_entry(store)
    register_anniversary_hook(AnnivCalendar(db))
    assert is_anniversary_today() is True
    db.close()


def test_register_hook_false_when_empty(tmp_path):
    db = init_core(tmp_path)
    register_anniversary_hook(AnnivCalendar(db))
    assert is_anniversary_today() is False
    db.close()


def test_unregister_restores_false(tmp_path):
    db = init_core(tmp_path)
    store = AnnivStore(db)
    _today_entry(store)
    register_anniversary_hook(AnnivCalendar(db))
    assert is_anniversary_today() is True
    unregister_anniversary_hook()
    assert is_anniversary_today() is False
    db.close()


def test_growth_anniversary_double(tmp_path):
    db = init_core(tmp_path)
    store = AnnivStore(db)
    _today_entry(store)
    register_anniversary_hook(AnnivCalendar(db))
    growth = PetGrowth(db, FakeSync(), MAC_KEY)
    assert growth.award("feed", now=DAY_TS) == 6  # 3 × 2（纪念日加成自动生效）
    db.close()
