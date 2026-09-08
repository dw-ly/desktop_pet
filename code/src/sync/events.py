"""事件类型与数据结构（对应 impl §2）。

事件类型唯一登记处为 spec/sync-security §3.3 注册表；本模块仅是落地，
新增/改名必须先改 spec。事件负载中的二进制一律为 base64 字符串（§1.2）。
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from enum import Enum

from .constants import PROTOCOL_VERSION
from .errors import ProtocolError

_JSON_KEYS = ("v", "type", "from", "seq", "ts", "payload")


class EventType(str, Enum):
    """事件类型注册表（spec §3.3）。"""

    HELLO = "hello"
    HELLO_ACK = "hello.ack"
    PAIRING_REVOKE = "pairing.revoke"
    MSG_CARRY = "msg.carry"
    CARRY_ACK = "carry.ack"
    CARRY_REVOKE = "carry.revoke"
    MOOD_SYNC = "mood.sync"
    PET_FEED = "pet.feed"
    PET_PROFILE = "pet.profile"
    DATE_ADD = "date.add"
    DATE_REMIND = "date.remind"
    DATE_SYNC = "date.sync"
    GIFT_SEND = "gift.send"
    GIFT_ACCEPT = "gift.accept"
    GIFT_EXPIRE = "gift.expire"


# 同步层内部类型（manager 处理，不投递 Core）
INTERNAL_TYPES = frozenset(
    {
        EventType.HELLO,
        EventType.HELLO_ACK,
        EventType.PAIRING_REVOKE,
    }
)

# 业务类型（分发到 add_handler 注册的 handler）
BUSINESS_TYPES = frozenset(t for t in EventType if t not in INTERNAL_TYPES)


@dataclass(frozen=True)
class Message:
    """密文内明文负载（impl §2.2）。"""

    v: int            # 协议版本 == PROTOCOL_VERSION
    type: str         # EventType 值
    from_id: str      # 发送方 peer_id
    seq: int          # 发送方单调递增序号
    ts: int           # 发送方 Unix 秒
    payload: dict     # 各类型自定义负载


@dataclass(frozen=True)
class Envelope:
    """传输层信封（impl §2.3），中继可见部分。"""

    to: str           # 目标 peer_id
    cipher: bytes     # SecretBox 密文（含 nonce）
    expires: int      # 过期时间 Unix 秒；0 表示不设过期


def message_to_json(m: Message) -> bytes:
    """Message → UTF-8 JSON，键序固定（impl §2.4）。"""
    obj = {
        "v": m.v,
        "type": m.type,
        "from": m.from_id,
        "seq": m.seq,
        "ts": m.ts,
        "payload": m.payload,
    }
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def message_from_json(raw: bytes) -> Message:
    """JSON → Message；未知类型 / 版本不兼容 / 坏 JSON 抛 ProtocolError。"""
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"坏 JSON: {exc}") from exc
    if not isinstance(obj, dict) or not all(k in obj for k in _JSON_KEYS):
        raise ProtocolError("消息缺少必需字段")
    try:
        etype = EventType(obj["type"])
    except ValueError as exc:
        raise ProtocolError(f"未知事件类型: {obj['type']!r}") from exc
    if obj["v"] != PROTOCOL_VERSION:
        raise ProtocolError(f"协议版本不兼容: {obj['v']} != {PROTOCOL_VERSION}")
    if not isinstance(obj["payload"], dict):
        raise ProtocolError("payload 必须是对象")
    return Message(
        v=obj["v"],
        type=etype.value,
        from_id=str(obj["from"]),
        seq=int(obj["seq"]),
        ts=int(obj["ts"]),
        payload=obj["payload"],
    )


def envelope_to_json(e: Envelope) -> bytes:
    """Envelope → UTF-8 JSON（cipher base64）。"""
    obj = {
        "to": e.to,
        "cipher": base64.b64encode(e.cipher).decode("ascii"),
        "expires": e.expires,
    }
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def envelope_from_json(raw: bytes) -> Envelope:
    """JSON → Envelope；坏 JSON / 缺字段抛 ProtocolError。"""
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"坏 JSON: {exc}") from exc
    if not isinstance(obj, dict) or "to" not in obj or "cipher" not in obj or "expires" not in obj:
        raise ProtocolError("信封缺少必需字段")
    try:
        cipher = base64.b64decode(obj["cipher"], validate=True)
    except (ValueError, TypeError) as exc:
        raise ProtocolError("信封 cipher 非法") from exc
    return Envelope(to=str(obj["to"]), cipher=cipher, expires=int(obj["expires"]))
