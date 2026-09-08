"""日历同步合并测试（anniversary impl §6.1 / plan S4）。"""

from __future__ import annotations

from core.anniv_sync import AnnivSync
from core.anniv_store import AnnivStore
from core.db import init_core
from sync.events import EventType, Message

TS = 1786000000


class FakeSync:
    def __init__(self, paired: bool = True) -> None:
        self.sent: list[tuple[EventType, dict]] = []
        self._paired = paired

    def send(self, type_, payload: dict, **kwargs) -> None:
        self.sent.append((type_, payload))

    def pairing_status(self) -> str:
        return "paired" if self._paired else "unpaired"


def _entry(**patch) -> dict:
    d = {
        "title": "在一起纪念日",
        "date": "02-14",
        "calendar": "solar",
        "repeat": "yearly",
        "notify_days_before": 1,
        "updated_at": TS,
    }
    d.update(patch)
    return d


def _setup(tmp_path, *, paired: bool = True):
    db = init_core(tmp_path)
    sync = FakeSync(paired=paired)
    store = AnnivStore(db)
    svc = AnnivSync(db, sync, store)
    return db, sync, store, svc


def _msg(payload: dict) -> Message:
    return Message(v=1, type="date.add", from_id="peer", seq=1,
                   ts=TS, payload=payload)


def test_add_paired_sends(tmp_path):
    db, sync, store, svc = _setup(tmp_path)
    entry = svc.add(_entry())
    assert store.get(entry["id"]) is not None
    assert len(sync.sent) == 1
    type_, payload = sync.sent[0]
    assert type_ == EventType.DATE_ADD
    assert payload["deleted"] is False
    assert set(payload) >= {"id", "title", "date", "calendar", "repeat",
                            "notify_days_before", "updated_at", "deleted"}
    db.close()


def test_add_unpaired_local_only(tmp_path):
    db, sync, store, svc = _setup(tmp_path, paired=False)
    entry = svc.add(_entry())
    assert store.get(entry["id"]) is not None
    assert sync.sent == []  # 未配对不外发
    db.close()


def test_update_sends(tmp_path):
    db, sync, store, svc = _setup(tmp_path)
    entry = svc.add(_entry())
    sync.sent.clear()
    updated = svc.update(entry["id"], {"title": "在一起 100 天"}, now=TS + 100)
    assert updated["title"] == "在一起 100 天"
    assert store.get(entry["id"])["updated_at"] == TS + 100
    assert len(sync.sent) == 1
    assert sync.sent[0][1]["updated_at"] == TS + 100
    db.close()


def test_delete_sends_tombstone(tmp_path):
    db, sync, store, svc = _setup(tmp_path)
    entry = svc.add(_entry())
    sync.sent.clear()
    assert svc.delete(entry["id"], now=TS + 200) is True
    assert store.get(entry["id"]) is None
    assert len(sync.sent) == 1
    type_, payload = sync.sent[0]
    assert type_ == EventType.DATE_ADD
    assert set(payload.keys()) == {"id", "deleted", "updated_at"}  # 最小墓碑
    assert payload["deleted"] is True
    assert payload["updated_at"] == TS + 200
    db.close()


def test_lww_newer_wins(tmp_path):
    db, sync, store, svc = _setup(tmp_path)
    entry = svc.add(_entry(updated_at=TS))  # 本地 ts=TS
    assert svc.handle_date_add(_msg(_entry(id=entry["id"], title="新标题", updated_at=TS + 500))) is True
    assert store.get(entry["id"])["title"] == "新标题"
    db.close()


def test_lww_older_ignored(tmp_path):
    db, sync, store, svc = _setup(tmp_path)
    entry = svc.add(_entry(updated_at=TS + 500))  # 本地 ts 更新
    assert svc.handle_date_add(_msg(_entry(id=entry["id"], title="旧标题", updated_at=TS))) is True
    assert store.get(entry["id"])["title"] == "在一起纪念日"  # 本地未变
    db.close()


def test_tombstone_lww(tmp_path):
    db, sync, store, svc = _setup(tmp_path)
    entry = svc.add(_entry(updated_at=TS))
    assert svc.handle_date_add(_msg({"id": entry["id"], "deleted": True,
                                     "updated_at": TS + 300})) is True
    assert store.get(entry["id"]) is None
    db.close()


def test_tombstone_old_ignored(tmp_path):
    db, sync, store, svc = _setup(tmp_path)
    entry = svc.add(_entry(updated_at=TS + 300))  # 本地 ts 更新
    assert svc.handle_date_add(_msg({"id": entry["id"], "deleted": True,
                                     "updated_at": TS})) is True
    assert store.get(entry["id"]) is not None  # 本地保留
    db.close()


def test_invalid_payloads(tmp_path):
    db, sync, store, svc = _setup(tmp_path)
    for bad in (
        {},
        {"title": "无 id"},
        {"id": "x", "updated_at": "not-int"},
        {"id": "x", "updated_at": 0},
        {"id": "x", "updated_at": TS, "date": "bad"},  # 字段非法
        "not-dict",
    ):
        assert svc.handle_date_add(_msg(bad)) is False
    assert store.list_all() == []
    db.close()
