"""配对流程（对应 impl §7 / plan S3）。

两步交换 + 单边 SAS 校验（防中间人）：
  1. B ── pair.request{pubB} ──► A
  2. A（用户确认后）── pair.accept{pubA, sas, version} ──► B
  3. B 计算 SAS 与 accept.sas 比对；A 在 UI 确认环节核对对方 peer_id。
  4. pairing_version 由 A 生成随 accept 下发，两端采用同一值（吊销一致性）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Callable

import websockets
from nacl.utils import random as nacl_random

from .config import SyncConfig
from .constants import CODE_TTL_SECONDS, MDNS_PAIR_SERVICE, PAIR_WS_TIMEOUT
from .crypto import (
    IdentityKeyPair,
    SessionKeys,
    _shared_secret,
    compute_sas,
    decode_pairing_code,
    derive_peer_id,
    derive_session,
    generate_pairing_code,
    random_session_id,
)
from .discovery import DiscoveryHandle, publish, resolve
from .errors import PairingError, TransportError
from .transport import _serve_on_free_port

log = logging.getLogger(__name__)

RequestCallback = Callable[[str, bytes], None]          # (peer_id, public_key)


@dataclass(frozen=True)
class PairingResult:
    peer_id: str
    peer_public: bytes
    session_keys: SessionKeys
    pairing_version: int


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


class PairingSession:
    """发起方（A）：创建一次性配对会话，等待 B 加入。

    会话属性：10 分钟有效、5 次尝试上限、成功后一次性消费（均由本对象强制执行）。
    """

    def __init__(
        self,
        cfg: SyncConfig,
        identity: IdentityKeyPair,
        my_peer_id: str,
        on_request: RequestCallback,
    ) -> None:
        self._cfg = cfg
        self._identity = identity
        self._my_peer_id = my_peer_id
        self._on_request = on_request

        self.session_id = random_session_id()
        self.code = generate_pairing_code(self.session_id)
        self._sid_name = f"pair-{self.session_id.hex()}"
        self.expires_at = int(time.time()) + CODE_TTL_SECONDS
        self.attempts = 0

        self._consumed = False
        self._server = None
        self._port: int | None = None
        self._handle: DiscoveryHandle | None = None
        self._ws = None
        self._pending_pub: bytes | None = None
        self._ttl_task: asyncio.Task | None = None

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at

    async def start(self) -> str:
        """启动临时监听并广播发现，返回短码。"""
        self._server, self._port = await _serve_on_free_port(
            self._handler, self._cfg.ws_port_range
        )
        # zeroconf 阻塞调用，不能直接在 asyncio loop 内执行（EventLoopBlocked）。
        self._handle = await asyncio.to_thread(
            publish, MDNS_PAIR_SERVICE, self._sid_name, self._port, {}
        )
        self._ttl_task = asyncio.create_task(self._ttl_guard())
        log.info("配对会话已启动，短码 %s", self.code)
        return self.code

    async def close(self) -> None:
        if self._ttl_task and not self._ttl_task.done():
            self._ttl_task.cancel()
        if self._handle:
            await asyncio.to_thread(self._handle.unpublish)
            self._handle = None
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._consumed = True

    async def _ttl_guard(self) -> None:
        wait = self.expires_at - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        log.info("配对会话超时关闭")
        await self.close()

    async def _handler(self, ws) -> None:
        self._ws = ws
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if msg.get("type") == "pair.request":
                    await self._handle_request(msg)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._ws = None

    async def _handle_request(self, msg: dict) -> None:
        if self._consumed or self.expired:
            await ws_safe_send(self._ws, json.dumps({"type": "pair.error", "reason": "expired"}))
            return
        self.attempts += 1
        if self.attempts > self._cfg.code_max_attempts:
            log.warning("配对尝试超次，会话关闭")
            await self.close()
            return
        try:
            pub = base64.b64decode(msg.get("pub") or "")
        except (ValueError, TypeError):
            return
        if len(pub) != 32:
            return
        self._pending_pub = pub
        peer_id = derive_peer_id(pub)
        log.info("收到配对请求 peer_id=%s", peer_id)
        # 同步回调，驱动 UI 确认（confirm / reject）
        self._on_request(peer_id, pub)

    async def confirm(self) -> PairingResult:
        """用户确认配对：发送 accept 并完成。会话一次性消费。"""
        if self._pending_pub is None:
            raise PairingError("尚未收到配对请求")
        if self._consumed:
            raise PairingError("该配对码已使用")
        peer_pub = self._pending_pub
        shared = _shared_secret(self._identity.private_key, peer_pub)
        sas = compute_sas(shared, self._identity.public_key, peer_pub)
        pairing_version = int.from_bytes(nacl_random(4), "big")
        await ws_safe_send(
            self._ws,
            json.dumps(
                {
                    "type": "pair.accept",
                    "pub": _b64(self._identity.public_key),
                    "sas": sas,
                    "version": pairing_version,
                }
            ),
        )
        keys = derive_session(self._identity.private_key, peer_pub)
        await self.close()
        return PairingResult(
            peer_id=derive_peer_id(peer_pub),
            peer_public=peer_pub,
            session_keys=keys,
            pairing_version=pairing_version,
        )

    async def reject(self) -> None:
        await ws_safe_send(self._ws, json.dumps({"type": "pair.error", "reason": "rejected"}))
        await self.close()


async def ws_safe_send(ws, payload: str) -> None:
    if ws is not None:
        try:
            await ws.send(payload)
        except websockets.ConnectionClosed:
            pass


class PairingClient:
    """加入方（B）：输入短码，完成配对。"""

    def __init__(self, cfg: SyncConfig, identity: IdentityKeyPair, my_peer_id: str) -> None:
        self._cfg = cfg
        self._identity = identity
        self._my_peer_id = my_peer_id

    async def join(self, code: str) -> PairingResult:
        session_id = decode_pairing_code(code)
        addr = await asyncio.to_thread(
            resolve,
            MDNS_PAIR_SERVICE,
            f"pair-{session_id.hex()}",
            min(3.0, PAIR_WS_TIMEOUT),
        )
        if addr is None:
            raise PairingError("未发现对方，请确认双方在同一网络")
        host, port = addr
        uri = f"ws://{host}:{port}"
        log.info("发现配对会话 %s:%s，连接中…", host, port)
        try:
            ws = await websockets.connect(uri, open_timeout=PAIR_WS_TIMEOUT)
        except OSError as exc:
            raise PairingError(f"连接对方失败: {exc}") from exc

        my_pub = self._identity.public_key
        try:
            await ws.send(json.dumps({"type": "pair.request", "pub": _b64(my_pub)}))
            raw = await asyncio.wait_for(ws.recv(), timeout=PAIR_WS_TIMEOUT)
            msg = json.loads(raw)
            if msg.get("type") != "pair.accept":
                raise PairingError(f"配对被拒绝: {msg.get('reason', '未知原因')}")
            pub_a = base64.b64decode(msg["pub"])
            if len(pub_a) != 32:
                raise PairingError("对方公钥非法")
            shared = _shared_secret(self._identity.private_key, pub_a)
            sas = compute_sas(shared, pub_a, my_pub)
            if sas != msg.get("sas"):
                raise PairingError("验证码不一致，疑似中间人，已中止配对")
            pairing_version = int(msg.get("version", 0))
            keys = derive_session(self._identity.private_key, pub_a)
            log.info("配对成功 peer_id=%s", derive_peer_id(pub_a))
            return PairingResult(
                peer_id=derive_peer_id(pub_a),
                peer_public=pub_a,
                session_keys=keys,
                pairing_version=pairing_version,
            )
        except asyncio.TimeoutError as exc:
            raise PairingError("配对超时") from exc
        finally:
            await ws.close()
