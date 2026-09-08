"""礼物数据层与状态流转（对应 gift-exchange impl §3 / plan S1）。

- gift_offers CRUD + 状态机（sent→accepted / sent→expired）
- user_items 解锁插入 / 查重 / 到期移除
- 自定义彩蛋列表：上限 20，超出替换最旧（rowid ASC）
- 乐观解锁：source='pending:{gift_id}'，接受后改为 'gift:{from}'，过期删除
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass

from core.db import Database
from core.gift_config import CUSTOM_EGG_ID, GiftState, LEGAL_TRANSITIONS


@dataclass
class GiftOffer:
    gift_id: str
    from_peer: str | None
    to_peer: str | None
    item_id: str | None
    state: GiftState
    sent_at: int | None
    expire_at: int | None
    accepted_at: int | None = None


@dataclass
class UserItem:
    item_id: str
    name: str | None
    type: str | None
    rarity: str | None
    source: str | None
    expire_at: int | None


class GiftStore:
    """gift_offers + user_items 读写封装。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------ #
    # 行转换
    # ------------------------------------------------------------------ #

    @staticmethod
    def _offer_from_row(row) -> GiftOffer:
        return GiftOffer(
            gift_id=row["gift_id"],
            from_peer=row["from_peer"],
            to_peer=row["to_peer"],
            item_id=row["item_id"],
            state=GiftState(row["state"]),
            sent_at=row["sent_at"],
            expire_at=row["expire_at"],
            accepted_at=row["accepted_at"],
        )

    @staticmethod
    def _item_from_row(row) -> UserItem:
        return UserItem(
            item_id=row["item_id"],
            name=row["name"],
            type=row["type"],
            rarity=row["rarity"],
            source=row["source"],
            expire_at=row["expire_at"],
        )

    # ------------------------------------------------------------------ #
    # gift_offers
    # ------------------------------------------------------------------ #

    def create_offer(
        self,
        *,
        from_peer: str,
        to_peer: str,
        item_id: str,
        sent_at: int,
        expire_at: int,
        gift_id: str | None = None,
    ) -> GiftOffer | None:
        """插入 sent 记录；同 gift_id 已存在 → None（幂等）。"""
        gid = gift_id or uuid.uuid4().hex
        try:
            self._db.execute(
                "INSERT INTO gift_offers"
                "(gift_id, from_peer, to_peer, item_id, state, sent_at, expire_at) "
                "VALUES(?, ?, ?, ?, 'sent', ?, ?)",
                (gid, from_peer, to_peer, item_id, sent_at, expire_at),
            )
        except sqlite3.IntegrityError:
            return None
        return self.get_offer(gid)

    def get_offer(self, gift_id: str) -> GiftOffer | None:
        row = self._db.query_one(
            "SELECT * FROM gift_offers WHERE gift_id=?", (gift_id,)
        )
        return self._offer_from_row(row) if row else None

    def list_offers(self, *, state: GiftState | None = None) -> list[GiftOffer]:
        if state is None:
            rows = self._db.query_all(
                "SELECT * FROM gift_offers ORDER BY sent_at"
            )
        else:
            rows = self._db.query_all(
                "SELECT * FROM gift_offers WHERE state=? ORDER BY sent_at",
                (state.value,),
            )
        return [self._offer_from_row(r) for r in rows]

    def pending_expired_offers(self, now: int) -> list[GiftOffer]:
        """超 TTL 仍为 sent 的记录（expire_at < now，严格小于）。"""
        rows = self._db.query_all(
            "SELECT * FROM gift_offers WHERE state='sent' AND expire_at < ? "
            "ORDER BY expire_at",
            (now,),
        )
        return [self._offer_from_row(r) for r in rows]

    def transition(
        self,
        gift_id: str,
        new_state: GiftState,
        *,
        accepted_at: int | None = None,
    ) -> bool:
        """合法状态流转；非法返回 False。"""
        offer = self.get_offer(gift_id)
        if offer is None:
            return False
        if new_state not in LEGAL_TRANSITIONS.get(offer.state, frozenset()):
            return False
        if new_state == GiftState.ACCEPTED:
            ts = accepted_at if accepted_at is not None else int(time.time())
            self._db.execute(
                "UPDATE gift_offers SET state=?, accepted_at=? WHERE gift_id=?",
                (new_state.value, ts, gift_id),
            )
        else:
            self._db.execute(
                "UPDATE gift_offers SET state=? WHERE gift_id=?",
                (new_state.value, gift_id),
            )
        return True

    def set_egg_text(self, gift_id: str, text: str) -> None:
        self._db.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"gift:egg:{gift_id}", text),
        )

    def get_egg_text(self, gift_id: str) -> str | None:
        row = self._db.query_one(
            "SELECT value FROM kv WHERE key=?", (f"gift:egg:{gift_id}",)
        )
        return str(row["value"]) if row else None

    def clear_egg_text(self, gift_id: str) -> None:
        self._db.execute("DELETE FROM kv WHERE key=?", (f"gift:egg:{gift_id}",))

    # ------------------------------------------------------------------ #
    # user_items
    # ------------------------------------------------------------------ #

    def unlock_item(
        self,
        *,
        item_id: str,
        name: str,
        item_type: str,
        rarity: str,
        source: str,
        expire_at: int | None = None,
    ) -> bool:
        """插入解锁；同 item_id 已存在 → False（once，不覆盖）。"""
        try:
            self._db.execute(
                "INSERT INTO user_items(item_id, name, type, rarity, source, expire_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (item_id, name, item_type, rarity, source, expire_at),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def get_item(self, item_id: str) -> UserItem | None:
        row = self._db.query_one(
            "SELECT * FROM user_items WHERE item_id=?", (item_id,)
        )
        return self._item_from_row(row) if row else None

    def list_available_items(self, now: int | None = None) -> list[UserItem]:
        """未到期可用物品（expire_at IS NULL OR expire_at > now）；排除 pending。"""
        ts = int(now) if now is not None else int(time.time())
        rows = self._db.query_all(
            "SELECT * FROM user_items WHERE "
            "(expire_at IS NULL OR expire_at > ?) "
            "AND (source IS NULL OR source NOT LIKE 'pending:%') "
            "ORDER BY item_id",
            (ts,),
        )
        return [self._item_from_row(r) for r in rows]

    def set_pending_unlock(
        self,
        *,
        item_id: str,
        name: str,
        item_type: str,
        rarity: str,
        gift_id: str,
        expire_at: int | None = None,
    ) -> bool:
        """发送端乐观解锁（source=pending:{gift_id}）。"""
        return self.unlock_item(
            item_id=item_id,
            name=name,
            item_type=item_type,
            rarity=rarity,
            source=f"pending:{gift_id}",
            expire_at=expire_at,
        )

    def finalize_pending(self, gift_id: str, source: str) -> bool:
        """pending → 正式 source；无 pending 行返回 False。"""
        cur = self._db.execute(
            "UPDATE user_items SET source=? WHERE source=?",
            (source, f"pending:{gift_id}"),
        )
        return cur.rowcount >= 1

    def remove_pending(self, gift_id: str) -> int:
        """过期回退乐观解锁。返回删除行数。"""
        cur = self._db.execute(
            "DELETE FROM user_items WHERE source=?", (f"pending:{gift_id}",)
        )
        return cur.rowcount

    def expire_items(self, now: int | None = None) -> list[str]:
        """移除 expire_at <= now 的物品；返回被移除 item_id 列表。"""
        ts = int(now) if now is not None else int(time.time())
        rows = self._db.query_all(
            "SELECT item_id FROM user_items WHERE expire_at IS NOT NULL "
            "AND expire_at <= ?",
            (ts,),
        )
        ids = [r["item_id"] for r in rows]
        if ids:
            self._db.execute(
                "DELETE FROM user_items WHERE expire_at IS NOT NULL AND expire_at <= ?",
                (ts,),
            )
        return ids

    # ------------------------------------------------------------------ #
    # 自定义彩蛋（上限 + 替换最旧）
    # ------------------------------------------------------------------ #

    def list_eggs(self) -> list[UserItem]:
        rows = self._db.query_all(
            "SELECT * FROM user_items WHERE type='custom_egg' ORDER BY rowid"
        )
        return [self._item_from_row(r) for r in rows]

    def add_egg(
        self,
        *,
        text: str,
        source: str,
        gift_id: str,
        egg_max: int = 20,
    ) -> str:
        """写入彩蛋；超出 egg_max 删除最旧。返回 item_id。"""
        eggs = self.list_eggs()
        while len(eggs) >= egg_max:
            oldest = eggs[0]
            self._db.execute(
                "DELETE FROM user_items WHERE item_id=?", (oldest.item_id,)
            )
            eggs = self.list_eggs()
        item_id = f"egg-{gift_id}"
        self.unlock_item(
            item_id=item_id,
            name=text,
            item_type="custom_egg",
            rarity="event",
            source=source,
            expire_at=None,
        )
        # 若同 gift 重复（幂等已存在），仍返回 id
        return item_id

    def egg_count(self) -> int:
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM user_items WHERE type='custom_egg'"
        )
        return int(row["n"]) if row else 0
