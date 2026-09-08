"""接收与打开确认（对应 gift-exchange impl §5 / plan S3）。

gift.send → 待打开收件箱 → 打开 → 验签 → 双方解锁 → gift.accept → 亲密度。
伪造签名拒绝解锁。"稍后"仅保留待打开，不阻塞。
"""

from __future__ import annotations

import time
from typing import Callable

from sync.events import EventType, Message

from .gift_config import (
    CUSTOM_EGG_ID,
    GiftConfig,
    GiftState,
    item_expire_at,
    load_gift_config,
    verify_unlock,
)
from .gift_intimacy import apply_gift_intimacy
from .gift_store import GiftOffer, GiftStore


class GiftReceive:
    """接收侧：handle_send / accept / later；发送侧：handle_accept。"""

    def __init__(
        self,
        store: GiftStore,
        sync,
        mac_key: bytes,
        *,
        cfg: GiftConfig | None = None,
        on_inbox: Callable[[GiftOffer], None] | None = None,
        on_unlocked: Callable[[str, str], None] | None = None,
        on_accepted: Callable[[GiftOffer], None] | None = None,
        on_forged: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._sync = sync
        self._mac_key = mac_key
        self._cfg = cfg or load_gift_config()
        self._on_inbox = on_inbox
        self._on_unlocked = on_unlocked
        self._on_accepted = on_accepted
        self._on_forged = on_forged

    def set_mac_key(self, mac_key: bytes) -> None:
        self._mac_key = mac_key

    # ------------------------------------------------------------------ #
    # 接收 gift.send
    # ------------------------------------------------------------------ #

    def handle_send(self, m: Message) -> bool:
        """处理 gift.send → 写入收件箱（sent）；非法负载丢弃。"""
        p = m.payload
        gift_id = p.get("giftId")
        item_id = p.get("item")
        to_peer = p.get("to")
        expire_at = p.get("expireAt")
        unlock_sig = p.get("unlockSig")
        if (
            not isinstance(gift_id, str)
            or not isinstance(item_id, str)
            or not isinstance(to_peer, str)
            or not isinstance(expire_at, int)
            or not isinstance(unlock_sig, str)
        ):
            return False

        # 先验签（在途即可校验；失败仍可入库待打开，但 accept 时再拒）
        offer = self._store.create_offer(
            gift_id=gift_id,
            from_peer=m.from_id,
            to_peer=to_peer,
            item_id=item_id,
            sent_at=m.ts,
            expire_at=expire_at,
        )
        if offer is None:
            # 幂等：已存在
            offer = self._store.get_offer(gift_id)
            if offer is None:
                return False
        else:
            egg = p.get("eggText")
            if isinstance(egg, str) and egg:
                self._store.set_egg_text(gift_id, egg)
            # 缓存 unlockSig 到 kv，accept 时用
            self._db_set_sig(gift_id, unlock_sig, expire_at)

        if self._on_inbox is not None and offer is not None:
            self._on_inbox(offer)
        return True

    def _db_set_sig(self, gift_id: str, sig: str, expire_at: int) -> None:
        self._store._db.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"gift:sig:{gift_id}", f"{sig}|{expire_at}"),
        )

    def _db_get_sig(self, gift_id: str) -> tuple[str, int] | None:
        row = self._store._db.query_one(
            "SELECT value FROM kv WHERE key=?", (f"gift:sig:{gift_id}",)
        )
        if not row:
            return None
        raw = str(row["value"])
        if "|" not in raw:
            return None
        sig, exp_s = raw.rsplit("|", 1)
        try:
            return sig, int(exp_s)
        except ValueError:
            return None

    # ------------------------------------------------------------------ #
    # 打开确认
    # ------------------------------------------------------------------ #

    def accept(
        self,
        gift_id: str,
        *,
        unlock_sig: str | None = None,
        now: float | None = None,
    ) -> bool:
        """打开礼物：验签 → 解锁 → gift.accept → 亲密度。伪造拒绝。"""
        offer = self._store.get_offer(gift_id)
        if offer is None or offer.state != GiftState.SENT:
            return False
        if not offer.item_id or not offer.from_peer:
            return False

        cached = self._db_get_sig(gift_id)
        exp = offer.expire_at or 0
        sig = unlock_sig
        if sig is None and cached:
            sig, exp = cached
        if not isinstance(sig, str):
            return False

        if not verify_unlock(self._mac_key, gift_id, offer.item_id, exp, sig):
            if self._on_forged is not None:
                self._on_forged(gift_id)
            return False

        ts = int(now) if now is not None else int(time.time())
        if not self._store.transition(
            gift_id, GiftState.ACCEPTED, accepted_at=ts
        ):
            return False

        self._persist_unlock(offer, ts)
        apply_gift_intimacy(self._store._db, self._mac_key)

        payload = {
            "giftId": gift_id,
            "item": offer.item_id,
            "unlockSig": sig,
        }
        self._sync.send(EventType.GIFT_ACCEPT, payload)

        if self._on_unlocked is not None:
            self._on_unlocked(gift_id, offer.item_id)
        return True

    def later(self, gift_id: str) -> bool:
        """稍后处理：保留待打开，不阻塞。存在且 sent → True。"""
        offer = self._store.get_offer(gift_id)
        return offer is not None and offer.state == GiftState.SENT

    def pending_inbox(self) -> list[GiftOffer]:
        """待打开列表（state=sent 且本端为接收方语义：有记录即可）。"""
        return self._store.list_offers(state=GiftState.SENT)

    # ------------------------------------------------------------------ #
    # 发送端收到 gift.accept
    # ------------------------------------------------------------------ #

    def handle_accept(self, m: Message) -> bool:
        """处理 gift.accept：验签 → 正式解锁 → 亲密度。"""
        p = m.payload
        gift_id = p.get("giftId")
        item_id = p.get("item")
        unlock_sig = p.get("unlockSig")
        if (
            not isinstance(gift_id, str)
            or not isinstance(item_id, str)
            or not isinstance(unlock_sig, str)
        ):
            return False

        offer = self._store.get_offer(gift_id)
        if offer is None or offer.state != GiftState.SENT:
            return False
        if offer.item_id != item_id:
            return False

        exp = offer.expire_at or 0
        if not verify_unlock(self._mac_key, gift_id, item_id, exp, unlock_sig):
            if self._on_forged is not None:
                self._on_forged(gift_id)
            return False

        ts = m.ts or int(time.time())
        if not self._store.transition(
            gift_id, GiftState.ACCEPTED, accepted_at=ts
        ):
            return False

        # 乐观 pending → 正式；若无 pending（彩蛋）则直接写入
        source = f"gift:{offer.from_peer or m.from_id}"
        if not self._store.finalize_pending(gift_id, source):
            self._persist_unlock(offer, ts, source=source)

        apply_gift_intimacy(self._store._db, self._mac_key)

        if self._on_accepted is not None:
            self._on_accepted(offer)
        if self._on_unlocked is not None:
            self._on_unlocked(gift_id, item_id)
        return True

    # ------------------------------------------------------------------ #
    # 解锁持久化
    # ------------------------------------------------------------------ #

    def _persist_unlock(
        self,
        offer: GiftOffer,
        now: int,
        *,
        source: str | None = None,
    ) -> None:
        item_id = offer.item_id or ""
        src = source or f"gift:{offer.from_peer or ''}"
        egg_text = self._store.get_egg_text(offer.gift_id)
        meta = self._cfg.get(item_id)

        if item_id == CUSTOM_EGG_ID or (meta and meta.type == "custom_egg") or egg_text:
            text = egg_text or (meta.name if meta else "彩蛋")
            self._store.add_egg(
                text=text,
                source=src,
                gift_id=offer.gift_id,
                egg_max=self._cfg.egg_max,
            )
            return

        if meta is None:
            # 未知物品仍解锁，用 item_id 作名
            self._store.unlock_item(
                item_id=item_id,
                name=item_id,
                item_type="outfit",
                rarity="common",
                source=src,
                expire_at=None,
            )
            return

        exp = item_expire_at(meta.expire_days, now)
        self._store.unlock_item(
            item_id=item_id,
            name=meta.name,
            item_type=meta.type,
            rarity=meta.rarity,
            source=src,
            expire_at=exp,
        )
