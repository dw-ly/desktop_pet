"""赠送流程（对应 gift-exchange impl §4 / plan S2）。

选择 → 二次确认（确认前可取消，不落库）→ 写 gift_offers(sent) →
gift.send 加密发送 → 本端乐观解锁（pending）→ 不可撤回。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Callable

from sync.events import EventType

from .gift_config import (
    CUSTOM_EGG_ID,
    GiftConfig,
    GiftItem,
    item_expire_at,
    load_gift_config,
    sign_unlock,
    validate_egg_text,
)
from .gift_store import GiftOffer, GiftStore


@dataclass
class GiftProposal:
    """确认前内存态（不落库）。"""

    item_id: str
    item: GiftItem
    egg_text: str | None = None


class GiftSend:
    """发送侧：propose / cancel / confirm。"""

    def __init__(
        self,
        store: GiftStore,
        sync,
        mac_key: bytes,
        *,
        cfg: GiftConfig | None = None,
        my_peer_id: Callable[[], str | None] | None = None,
        partner_id: Callable[[], str | None] | None = None,
        on_optimistic: Callable[[GiftOffer, GiftItem], None] | None = None,
    ) -> None:
        self._store = store
        self._sync = sync
        self._mac_key = mac_key
        self._cfg = cfg or load_gift_config()
        self._my_peer_id = my_peer_id or (lambda: None)
        self._partner_id = partner_id or (lambda: None)
        self._on_optimistic = on_optimistic
        self._pending: GiftProposal | None = None

    @property
    def config(self) -> GiftConfig:
        return self._cfg

    @property
    def pending(self) -> GiftProposal | None:
        return self._pending

    def catalog(self) -> list[GiftItem]:
        """可用礼物库（含 custom_egg 虚拟条目）。"""
        items = list(self._cfg.catalog)
        # 自定义彩蛋始终可选
        if self._cfg.get(CUSTOM_EGG_ID) and not any(
            i.id == CUSTOM_EGG_ID for i in items
        ):
            egg = self._cfg.get(CUSTOM_EGG_ID)
            assert egg is not None
            items.append(egg)
        return items

    def propose(
        self, item_id: str, *, egg_text: str | None = None
    ) -> GiftProposal | None:
        """进入二次确认；非法 item / 彩蛋校验失败 → None。"""
        item = self._cfg.get(item_id)
        if item is None:
            return None
        egg: str | None = None
        if item.type == "custom_egg" or item_id == CUSTOM_EGG_ID:
            egg = validate_egg_text(egg_text, self._cfg)
            if egg is None:
                return None
        elif egg_text:
            return None  # 非彩蛋不应带文本
        self._pending = GiftProposal(item_id=item_id, item=item, egg_text=egg)
        return self._pending

    def cancel(self) -> None:
        """确认前取消：清空内存态，不产生任何记录。"""
        self._pending = None

    def confirm(self, *, now: float | None = None) -> GiftOffer | None:
        """二次确认后发送。无 pending / 未配对 → None。发送后不可撤回。"""
        prop = self._pending
        if prop is None:
            return None
        me = self._my_peer_id()
        partner = self._partner_id()
        if not me or not partner:
            return None

        ts = int(now) if now is not None else int(time.time())
        gift_id = uuid.uuid4().hex
        offer_expire = ts + self._cfg.offer_ttl_seconds
        unlock_sig = sign_unlock(self._mac_key, gift_id, prop.item_id, offer_expire)

        offer = self._store.create_offer(
            gift_id=gift_id,
            from_peer=me,
            to_peer=partner,
            item_id=prop.item_id,
            sent_at=ts,
            expire_at=offer_expire,
        )
        if offer is None:
            return None

        if prop.egg_text:
            self._store.set_egg_text(gift_id, prop.egg_text)

        # 乐观解锁（彩蛋不写 user_items pending，接受时再写入记忆库）
        item_exp = item_expire_at(prop.item.expire_days, ts)
        if prop.item.type != "custom_egg":
            self._store.set_pending_unlock(
                item_id=prop.item_id,
                name=prop.item.name,
                item_type=prop.item.type,
                rarity=prop.item.rarity,
                gift_id=gift_id,
                expire_at=item_exp,
            )

        payload: dict = {
            "giftId": gift_id,
            "item": prop.item_id,
            "to": partner,
            "expireAt": offer_expire,
            "unlockSig": unlock_sig,
        }
        if prop.egg_text is not None:
            payload["eggText"] = prop.egg_text

        self._sync.send(EventType.GIFT_SEND, payload)
        self._pending = None

        if self._on_optimistic is not None:
            self._on_optimistic(offer, prop.item)
        return offer
