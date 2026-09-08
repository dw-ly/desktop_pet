"""等级与解锁系统测试（pet-growth impl §5.2 / plan S3）。"""

from __future__ import annotations

from core.db import init_core
from core.pet_level import PetLevel


def _intimacy(db) -> int:
    row = db.query_one("SELECT value FROM pet_state WHERE key='intimacy'")
    return int(row["value"]) if row else 0


def _unlock_count(db) -> int:
    return db.query_one(
        "SELECT COUNT(*) AS n FROM user_items WHERE source LIKE 'unlock:%'"
    )["n"]


def test_initial_level_zero(tmp_path) -> None:
    db = init_core(tmp_path)
    assert PetLevel(db).current_level() == 0
    db.close()


def test_level_milestone_unlock(tmp_path) -> None:
    db = init_core(tmp_path)
    pl = PetLevel(db)
    newly = pl.sync(intimacy=2500)  # level 5
    assert "unlock_level_5" in newly
    assert pl.current_level() == 5
    db.close()


def test_intimacy_milestone_unlock(tmp_path) -> None:
    db = init_core(tmp_path)
    pl = PetLevel(db)
    newly = pl.sync(intimacy=100)
    assert "unlock_intimacy_100" in newly
    db.close()


def test_bulk_level_up_fills_all(tmp_path) -> None:
    db = init_core(tmp_path)
    pl = PetLevel(db)
    newly = pl.sync(intimacy=10000)  # level 10：level5 + level10 + intimacy 100/365/1000
    assert set(newly) == {
        "unlock_level_5",
        "unlock_level_10",
        "unlock_intimacy_100",
        "unlock_intimacy_365",
        "unlock_intimacy_1000",
    }
    assert "unlock_level_15" not in newly  # 22500 未到
    assert pl.current_level() == 10
    db.close()


def test_level_never_decreases(tmp_path) -> None:
    db = init_core(tmp_path)
    pl = PetLevel(db)
    pl.sync(intimacy=2500)
    assert pl.current_level() == 5
    pl.sync(intimacy=2000)  # intimacy 回退（兜底修复场景）
    assert pl.current_level() == 5  # 镜像只增
    assert _unlock_count(db) == 4  # 解锁不删除
    db.close()


def test_sync_idempotent(tmp_path) -> None:
    db = init_core(tmp_path)
    pl = PetLevel(db)
    first = pl.sync(intimacy=2500)
    assert len(first) == 4
    assert pl.sync(intimacy=2500) == []  # 第 2 次无新增
    assert _unlock_count(db) == 4  # 无重复行
    db.close()


def test_unlocked_items_ordered(tmp_path) -> None:
    db = init_core(tmp_path)
    pl = PetLevel(db)
    pl.sync(intimacy=2500)
    items = pl.unlocked_items()
    assert [i["item_id"] for i in items] == [
        "unlock_level_5",
        "unlock_intimacy_100",
        "unlock_intimacy_365",
        "unlock_intimacy_1000",
    ]
    assert all(i["source"].startswith("unlock:") for i in items)
    db.close()


def test_sync_from_db_intimacy(tmp_path) -> None:
    db = init_core(tmp_path)
    db.execute("INSERT INTO pet_state(key, value) VALUES('intimacy', 100)")
    pl = PetLevel(db)
    assert pl.sync() == ["unlock_intimacy_100"]
    assert _intimacy(db) == 100
    db.close()


def test_zero_sync_no_writes(tmp_path) -> None:
    db = init_core(tmp_path)
    pl = PetLevel(db)
    assert pl.sync(intimacy=0) == []
    assert _unlock_count(db) == 0
    assert pl.current_level() == 0
    db.close()
