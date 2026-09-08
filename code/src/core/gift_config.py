"""礼物库加载与配置（对应 gift-exchange impl §2 / plan G1）。

- 从仓库根 `assets/manifest.json` 加载 17 款礼物（id/name/type/rarity/expire_days）
- 类型：outfit|action|emoji|custom_egg；状态：sent|accepted|expired
- 配置：offer TTL 24h、彩蛋上限 20、彩蛋长度 ≤200
- manifest 缺失/损坏 → 空礼物库（可运行兜底）
- 解锁凭证 HMAC 签名（与 intimacy 同源 MAC 键）
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)

# code/src/core/gift_config.py → parents[3] = 仓库根 desktop_pet/
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_MANIFEST = _REPO_ROOT / "assets" / "manifest.json"

GIFT_TYPES: tuple[str, ...] = ("outfit", "action", "emoji", "custom_egg")
CUSTOM_EGG_ID = "custom-egg"


class GiftState(str, Enum):
    SENT = "sent"
    ACCEPTED = "accepted"
    EXPIRED = "expired"


LEGAL_TRANSITIONS: dict[GiftState, frozenset[GiftState]] = {
    GiftState.SENT: frozenset({GiftState.ACCEPTED, GiftState.EXPIRED}),
    GiftState.ACCEPTED: frozenset(),
    GiftState.EXPIRED: frozenset(),
}


@dataclass(frozen=True)
class GiftItem:
    """礼物库条目（manifest 一行）。"""

    id: str
    name: str
    type: str
    rarity: str
    expire_days: int
    asset: str = ""


@dataclass(frozen=True)
class GiftConfig:
    """互赠配置（impl §2.3）。"""

    offer_ttl_seconds: int = 86400  # 24h 未接受 → expire
    egg_max: int = 20
    egg_max_len: int = 200
    catalog: tuple[GiftItem, ...] = field(default_factory=tuple)
    manifest_path: str = ""

    def get(self, item_id: str) -> GiftItem | None:
        for g in self.catalog:
            if g.id == item_id:
                return g
        if item_id == CUSTOM_EGG_ID:
            return GiftItem(
                id=CUSTOM_EGG_ID,
                name="自定义彩蛋",
                type="custom_egg",
                rarity="event",
                expire_days=0,
            )
        return None

    def by_type(self, gift_type: str) -> list[GiftItem]:
        return [g for g in self.catalog if g.type == gift_type]


def _parse_item(raw: dict) -> GiftItem | None:
    try:
        gid = str(raw["id"])
        gtype = str(raw["type"])
        if gtype not in GIFT_TYPES:
            log.warning("跳过未知礼物类型 %s id=%s", gtype, gid)
            return None
        return GiftItem(
            id=gid,
            name=str(raw.get("name", gid)),
            type=gtype,
            rarity=str(raw.get("rarity", "common")),
            expire_days=int(raw.get("expire_days", 0)),
            asset=str(raw.get("asset", "")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("跳过非法礼物条目 %r: %s", raw, exc)
        return None


def load_gift_catalog(manifest_path: str | Path | None = None) -> tuple[GiftItem, ...]:
    """加载 manifest；缺失/损坏返回空元组。"""
    path = Path(manifest_path) if manifest_path else _DEFAULT_MANIFEST
    if not path.is_file():
        log.warning("礼物 manifest 缺失: %s → 空礼物库", path)
        return ()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("礼物 manifest 损坏 %s: %s → 空礼物库", path, exc)
        return ()
    gifts = data.get("gifts") if isinstance(data, dict) else None
    if not isinstance(gifts, list):
        log.warning("礼物 manifest 无 gifts 列表 → 空礼物库")
        return ()
    items: list[GiftItem] = []
    for raw in gifts:
        if isinstance(raw, dict):
            item = _parse_item(raw)
            if item is not None:
                items.append(item)
    return tuple(items)


def load_gift_config(
    overrides: dict | None = None,
    *,
    manifest_path: str | Path | None = None,
) -> GiftConfig:
    """配置加载：缺失键用默认值；manifest 路径可覆盖。"""
    ov = dict(overrides or {})
    path = ov.pop("manifest_path", None) or manifest_path
    path_obj = Path(path) if path else _DEFAULT_MANIFEST
    catalog = load_gift_catalog(path_obj)

    kwargs: dict = {"catalog": catalog, "manifest_path": str(path_obj)}
    if "offer_ttl_seconds" in ov:
        ttl = int(ov["offer_ttl_seconds"])
        if ttl <= 0:
            raise ValueError(f"offer_ttl_seconds 必须 > 0: {ov['offer_ttl_seconds']!r}")
        kwargs["offer_ttl_seconds"] = ttl
    if "egg_max" in ov:
        em = int(ov["egg_max"])
        if em <= 0:
            raise ValueError(f"egg_max 必须 > 0: {ov['egg_max']!r}")
        kwargs["egg_max"] = em
    if "egg_max_len" in ov:
        el = int(ov["egg_max_len"])
        if el <= 0:
            raise ValueError(f"egg_max_len 必须 > 0: {ov['egg_max_len']!r}")
        kwargs["egg_max_len"] = el
    return GiftConfig(**kwargs)


def validate_egg_text(text: str | None, cfg: GiftConfig | None = None) -> str | None:
    """校验彩蛋文本；合法返回去空白后文本，非法返回 None。"""
    cfg = cfg or GiftConfig()
    if text is None:
        return None
    s = text.strip()
    if not s or len(s) > cfg.egg_max_len:
        return None
    return s


# --------------------------------------------------------------------------- #
# 解锁凭证签名（HMAC-SHA256，与 intimacy 同源 MAC 键）
# --------------------------------------------------------------------------- #


def _unlock_canonical(gift_id: str, item_id: str, expire_at: int) -> bytes:
    return f"{gift_id}:{item_id}:{int(expire_at)}".encode("ascii")


def sign_unlock(mac_key: bytes, gift_id: str, item_id: str, expire_at: int) -> str:
    """HMAC-SHA256(mac_key, f"{giftId}:{item}:{expireAt}")，hex 输出。"""
    return hmac.new(
        mac_key, _unlock_canonical(gift_id, item_id, expire_at), hashlib.sha256
    ).hexdigest()


def verify_unlock(
    mac_key: bytes, gift_id: str, item_id: str, expire_at: int, sig: str
) -> bool:
    """恒定时间比较；非法参数一律 False。"""
    if not isinstance(sig, str) or not sig:
        return False
    try:
        expect = sign_unlock(mac_key, gift_id, item_id, expire_at)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expect, sig)


def item_expire_at(expire_days: int, now: int) -> int | None:
    """expire_days<=0 → 永久（None）；否则 now + days*86400。"""
    if expire_days <= 0:
        return None
    return int(now) + int(expire_days) * 86400
