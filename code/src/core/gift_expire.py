"""过期退回与解锁失效（对应 gift-exchange impl §6 / plan S4）。

扫描 gift_offers 超 TTL 仍 sent → gift.expire → 两端置 expired；
user_items 按 expire_at 到期移除；发送端回退乐观解锁。
"""

from __future__ import annotations

import time
from typing import Callable

from sync.events import EventType, Message

from .gift_config import GiftConfig, GiftState, load_gift_config
from .gift_store import GiftOffer, GiftStore


class GiftExpire:
    """过期扫描 + gift.expire 收发 + 物品到期清理。"""

    def __init__(
        self,
        store: GiftStore,
        sync,
        *,
        cfg: GiftConfig | None = None,
        on_offer_expired: Callable[[GiftOffer], None] | None = None,
        on_items_expired: Callable[[list[str]], None] | None = None,
    ) -> None:
        self._store = store
        self._sync = sync
        self._cfg = cfg or load_gift_config()
        self._on_offer_expired = on_offer_expired
        self._on_items_expired = on_items_expired

    def scan_offers(self, now: float | None = None) -> list[str]:
        """扫描并过期退回；返回被过期的 gift_id 列表。本端主动发 gift.expire。"""
        ts = int(now) if now is not None else int(time.time())
        expired_ids: list[str] = []
        for offer in self._store.pending_expired_offers(ts):
            if self._expire_local(offer, send=True):
                expired_ids.append(offer.gift_id)
        return expired_ids

    def expire_items(self, now: float | None = None) -> list[str]:
        """清理到期 user_items；返回被移除 item_id。"""
        ts = int(now) if now is not None else int(time.time())
        removed = self._store.expire_items(ts)
        if removed and self._on_items_expired is not None:
            self._on_items_expired(removed)
        return removed

    def tick(self, now: float | None = None) -> dict:
        """一次完整扫描：offers + items。"""
        return {
            "offers": self.scan_offers(now),
            "items": self.expire_items(now),
        }

    def handle_expire(self, m: Message) -> bool:
        """接收 gift.expire → 本端置 expired + 清理 pending/inbox。"""
        gift_id = m.payload.get("giftId")
        if not isinstance(gift_id, str):
            return False
        offer = self._store.get_offer(gift_id)
        if offer is None:
            return False
        if offer.state == GiftState.EXPIRED:
            return True  # 幂等
        if offer.state != GiftState.SENT:
            return False
        return self._expire_local(offer, send=False)

    def _expire_local(self, offer: GiftOffer, *, send: bool) -> bool:
        if not self._store.transition(offer.gift_id, GiftState.EXPIRED):
            return False
        self._store.remove_pending(offer.gift_id)
        self._store.clear_egg_text(offer.gift_id)
        # 清理签名缓存
        self._store._db.execute(
            "DELETE FROM kv WHERE key=?", (f"gift:sig:{offer.gift_id}",)
        )
        if send:
            self._sync.send(EventType.GIFT_EXPIRE, {"giftId": offer.gift_id})
        if self._on_offer_expired is not None:
            self._on_offer_expired(offer)
        return True
