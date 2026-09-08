"""共同养成 等级与解锁系统（对应 pet-growth impl §5 / plan S3）。

- `PetLevel.sync`：亲密度变化后调用——按等级曲线换算等级（`level =
  int(sqrt(intimacy/k))`，D26：exp 镜像亲密度，不单独存 exp）并补发解锁。
- 解锁物品持久化到 `user_items`（`source='unlock:<kind>:<threshold>'`，D23）：
  备份三表之一、换机随密文恢复、可随时从 level/intimacy 重算补发。
- **只增不降**：等级镜像取 max（daily_align 兜底修复 intimacy 瞬时回退不触发
  降级）；解锁 `INSERT OR IGNORE` 永不删除。越级（批量经验一次跨多里程碑）一次
  补发全部新增解锁，返回完整新增列表。
"""

from __future__ import annotations

from .pet_config import (
    UNLOCK_ITEMS,
    PetConfig,
    earned_unlock_ids,
    level_from_intimacy,
    load_pet_config,
)

# 解锁返回/列表的规范顺序：等级里程碑（升）→ 亲密度里程碑（升）
_UNLOCK_ORDER: tuple[str, ...] = (
    "unlock_level_5",
    "unlock_level_10",
    "unlock_level_15",
    "unlock_intimacy_100",
    "unlock_intimacy_365",
    "unlock_intimacy_1000",
)


def _intimacy(db) -> int:
    row = db.query_one("SELECT value FROM pet_state WHERE key='intimacy'")
    return int(row["value"]) if row else 0


class PetLevel:
    """等级换算（镜像只增）+ 解锁持久化（user_items，source='unlock:...'）。"""

    def __init__(self, db, cfg: PetConfig | None = None) -> None:
        self._db = db
        self._cfg = cfg or load_pet_config()

    def current_level(self) -> int:
        """pet_state.level 镜像；缺失按 intimacy 计算（启动时未 sync 也正确）。"""
        row = self._db.query_one("SELECT value FROM pet_state WHERE key='level'")
        if row is not None:
            return int(row["value"])
        return level_from_intimacy(_intimacy(self._db), self._cfg.level_k)

    def sync(self, intimacy: int | None = None) -> list[str]:
        """亲密度变化后调用：重算等级并补发解锁。返回本次新增解锁 item_id 列表。"""
        if intimacy is None:
            intimacy = _intimacy(self._db)
        level_new = level_from_intimacy(intimacy, self._cfg.level_k)

        # 等级镜像（只增不降）
        if level_new > self.current_level():
            self._db.execute(
                "INSERT INTO pet_state(key, value) VALUES('level', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (level_new,),
            )

        # 补发解锁（越级一次补发全部，返回完整新增列表）
        earned = earned_unlock_ids(self._cfg, level_new, intimacy)
        newly: list[str] = []
        for item_id in _UNLOCK_ORDER:
            if item_id not in earned:
                continue
            if self._insert_unlock(item_id, UNLOCK_ITEMS[item_id]):
                newly.append(item_id)
        return newly

    def unlocked_items(self) -> list[dict]:
        """已解锁列表（user_items WHERE source LIKE 'unlock:%'），按阈值升序。"""
        rows = self._db.query_all(
            "SELECT item_id, name, type, rarity, source FROM user_items "
            "WHERE source LIKE 'unlock:%'"
        )
        by_id = {r["item_id"]: dict(r) for r in rows}

        def sort_key(item_id: str) -> tuple[int, int]:
            try:
                return (0, _UNLOCK_ORDER.index(item_id))
            except ValueError:
                return (1, 0)

        return [by_id[i] for i in sorted(by_id, key=sort_key)]

    def _insert_unlock(self, item_id: str, meta: dict) -> bool:
        cur = self._db.execute(
            "INSERT OR IGNORE INTO user_items(item_id, name, type, rarity, source) "
            "VALUES(?, ?, ?, ?, ?)",
            (
                item_id,
                meta["name"],
                meta["type"],
                meta["rarity"],
                f"unlock:{meta['kind']}:{meta['threshold']}",
            ),
        )
        return cur.rowcount == 1
