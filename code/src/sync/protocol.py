"""协议编解码与序号（对应 impl §3 / plan G1+S4）。

- SeqManager：发送端单调递增序号，持久化防回退
- dedup_key：接收端去重键（发送方标识 + 序号）
- 协议版本兼容校验
"""

from __future__ import annotations

from typing import Callable

from .constants import PROTOCOL_VERSION
from .errors import ProtocolError


class SeqManager:
    """发送端单调递增序号。

    meta_get / meta_set 由调用方（queue）提供，操作持久化 kv 存储；
    保证崩溃重启后序号不回退（宁可跳号，不可重复分配）。
    """

    def __init__(
        self,
        peer_id: str,
        meta_get: Callable[[str], str | None],
        meta_set: Callable[[str, str], None],
    ) -> None:
        self._key = f"{peer_id}:next_seq"
        self._meta_get = meta_get
        self._meta_set = meta_set

    def next(self) -> int:
        cur = int(self._meta_get(self._key) or "1")
        # 先持久化游标再返回，崩溃后不会重复分配已返回的序号
        self._meta_set(self._key, str(cur + 1))
        return cur


def dedup_key(from_id: str, seq: int) -> str:
    """去重键（impl §3.2）。与 data-consistency 的 event_id 同构。"""
    return f"{from_id}:{seq}"


def check_version(v: int) -> None:
    if v != PROTOCOL_VERSION:
        raise ProtocolError(f"协议版本不兼容: {v} != {PROTOCOL_VERSION}")
