"""纪念日数据模型测试（anniversary impl §2.3 / plan G1）。"""

from __future__ import annotations

import pytest

from core.anniv_store import AnnivStore
from core.db import init_core


def _entry(**patch) -> dict:
    d = {
        "title": "在一起纪念日",
        "date": "02-14",
        "calendar": "solar",
        "repeat": "yearly",
        "notify_days_before": 1,
        "updated_at": 100,
    }
    d.update(patch)
    return d


def test_add_defaults_filled(tmp_path):
    db = init_core(tmp_path)
    store = AnnivStore(db)
    entry = store.add(_entry())
    assert entry["id"]
    assert entry["updated_at"] > 0
    got = store.get(entry["id"])
    assert got is not None
    assert got["title"] == "在一起纪念日"
    assert got["calendar"] == "solar"
    db.close()


def test_add_duplicate_id_rejected(tmp_path):
    db = init_core(tmp_path)
    store = AnnivStore(db)
    store.add(_entry(id="x1"))
    with pytest.raises(ValueError):
        store.add(_entry(id="x1"))
    db.close()


def test_invalid_fields(tmp_path):
    db = init_core(tmp_path)
    store = AnnivStore(db)
    for bad in (
        {"title": ""},
        {"calendar": "hebrew"},
        {"repeat": "weekly"},
        {"date": ""},
        {"notify_days_before": -1},
        {"notify_days_before": "1"},
    ):
        with pytest.raises(ValueError):
            store.add(_entry(**bad))
    db.close()


def test_once_date_validation(tmp_path):
    db = init_core(tmp_path)
    store = AnnivStore(db)
    store.add(_entry(repeat="once", date="2026-11-11"))
    with pytest.raises(ValueError):
        store.add(_entry(repeat="once", date="2026-02-30"))  # 真实日期校验
    with pytest.raises(ValueError):
        store.add(_entry(repeat="once", date="02-14"))  # 格式错误
    db.close()


def test_yearly_date_validation(tmp_path):
    db = init_core(tmp_path)
    store = AnnivStore(db)
    store.add(_entry(repeat="yearly", date="02-29"))  # 2000 闰年基准允许
    with pytest.raises(ValueError):
        store.add(_entry(repeat="yearly", date="13-40"))
    with pytest.raises(ValueError):
        store.add(_entry(repeat="yearly", date="2026-02-14"))  # 格式错误
    db.close()


def test_lunar_once(tmp_path):
    db = init_core(tmp_path)
    store = AnnivStore(db)
    entry = store.add(_entry(repeat="once", date="2024-07-07", calendar="lunar"))
    got = store.get(entry["id"])
    assert got["calendar"] == "lunar"
    assert got["date"] == "2024-07-07"
    db.close()


def test_update_refresh_ts(tmp_path):
    db = init_core(tmp_path)
    store = AnnivStore(db)
    entry = store.add(_entry(updated_at=100))
    updated = store.update(entry["id"], {"title": "在一起 100 天"}, now=200)
    assert updated is not None
    assert updated["title"] == "在一起 100 天"
    assert updated["updated_at"] == 200
    assert updated["date"] == "02-14"  # 其余列保留
    db.close()


def test_update_nonexistent(tmp_path):
    db = init_core(tmp_path)
    store = AnnivStore(db)
    assert store.update("nope", {}) is None
    db.close()


def test_delete(tmp_path):
    db = init_core(tmp_path)
    store = AnnivStore(db)
    entry = store.add(_entry())
    assert store.delete(entry["id"]) is True
    assert store.delete(entry["id"]) is False
    db.close()


def test_list_all_sorted(tmp_path):
    db = init_core(tmp_path)
    store = AnnivStore(db)
    for ts, title in ((300, "c"), (100, "a"), (200, "b")):
        store.add(_entry(title=title, updated_at=ts))
    titles = [e["title"] for e in store.list_all()]
    assert titles == ["a", "b", "c"]
    db.close()


def test_upsert_overwrite(tmp_path):
    db = init_core(tmp_path)
    store = AnnivStore(db)
    entry = store.add(_entry(id="x1", title="旧标题"))
    assert store.upsert(_entry(id="x1", title="新标题", updated_at=999))["title"] == "新标题"
    assert len(store.list_all()) == 1  # 无重复行
    assert store.get("x1")["updated_at"] == 999
    db.close()
