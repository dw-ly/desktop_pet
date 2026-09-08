"""亲密度联动（对应 gift-exchange impl §7 / plan S5）。

礼物被接受后双方各计一次：普通 +5 / 纪念日 +20（不叠加 ×2）。
走 apply_intimacy_event 唯一入口（本地合入，不另发 pet.feed，避免双计）。
"""

from __future__ import annotations

import time

from .consistency import apply_intimacy_event, sign_intimacy_event
from .pet_config import PetConfig, load_pet_config
from .pet_growth import is_anniversary_today


def gift_intimacy_delta(cfg: PetConfig | None = None) -> int:
    """当前应加亲密度（纪念日特惠判定）。"""
    cfg = cfg or load_pet_config()
    if is_anniversary_today():
        return cfg.gift_anniversary_delta
    return cfg.gift_delta


def apply_gift_intimacy(
    db,
    mac_key: bytes,
    *,
    cfg: PetConfig | None = None,
    now: float | None = None,
) -> int:
    """合入一次礼物亲密度。返回实际 delta（0=拒绝）。

    仅本地 apply_intimacy_event，**不**发送 pet.feed——双方各自在
    accept / handle_accept 时各计一次，避免对端 PetSync 再合入导致双倍。
    """
    cfg = cfg or load_pet_config()
    amount = gift_intimacy_delta(cfg)
    ts = int(now) if now is not None else int(time.time())
    ev = {
        "delta": amount,
        "reason": "gift",
        "ts": ts,
        "sig": sign_intimacy_event(mac_key, amount, "gift", ts),
    }
    if apply_intimacy_event(db, ev, mac_key):
        return amount
    return 0
