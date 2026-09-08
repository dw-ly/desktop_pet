"""同步管理器（对应 impl §10 / plan S7）。

- 独立线程 + asyncio 事件循环
- send：未配对禁止外发；在线直发 / 离线入队
- 接收：解密 → 去重 → 分发（内部类型自处理，业务类型走 add_handler）
- 心跳握手与配对版本校验（吊销一致性）
- 配对 / 吊销全流程编排
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import threading
import time
from collections import defaultdict
from enum import Enum
from pathlib import Path
from typing import Callable, Coroutine, DefaultDict

from nacl.public import PrivateKey

from .config import SyncConfig
from .crypto import (
    IdentityKeyPair,
    SessionKeys,
    derive_peer_id,
    derive_session,
    encrypt,
    decrypt,
    generate_identity,
)
from .errors import CryptoError, PairingError, ProtocolError, SyncError
from .events import EventType, Envelope, Message, message_from_json, message_to_json
from .key_store import KeyStore
from .pairing import PairingClient, PairingResult, PairingSession
from .queue import SyncQueue, migrate_legacy_queue
from .transport import ConnState, LanTransport, Transport
from .constants import PROTOCOL_VERSION

log = logging.getLogger(__name__)

EventHandler = Callable[[Message], None]


class PairingEvent(str, Enum):
    PEER_REQUEST = "peer_request"        # A 收到请求，UI 弹确认框
    PAIRED = "paired"
    REVOKED = "revoked"                  # 本端发起吊销 / 版本不一致判吊销
    REVOKE_RECEIVED = "revoke_received"  # 收到对方 pairing.revoke


class PairingStatus(str, Enum):
    UNPAIRED = "unpaired"
    PAIRED = "paired"


class SyncManager:
    """同步层门面。除 start/stop 外，业务方法均同步阻塞等待 loop 线程结果。

    db：统一 core.db（Database），由 Core 启动时 init_core 创建后注入；
    收编后发送队列/接收去重/序号游标均落在 core.db（data-consistency S1/S2）。
    """

    def __init__(
        self,
        cfg: SyncConfig,
        *,
        db,
        on_event: Callable[[Message], None] | None = None,
        on_state: Callable[[ConnState], None] | None = None,
        on_pairing: Callable[[PairingEvent, dict], None] | None = None,
    ) -> None:
        self._cfg = cfg
        self._db = db
        self._on_event = on_event or (lambda m: None)
        self._on_state = on_state or (lambda s: None)
        self._on_pairing = on_pairing or (lambda e, d: None)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop_evt: asyncio.Event | None = None

        # 状态（只在 loop 线程写）
        self._queue: SyncQueue | None = None
        self._key_store: KeyStore | None = None
        self._identity: IdentityKeyPair | None = None
        self._my_peer_id = ""
        self._peer_id: str | None = None
        self._peer_public: bytes | None = None
        self._session_keys: SessionKeys | None = None
        self._pairing_version: int | None = None
        self._transport: Transport | None = None
        self._pairing_session: PairingSession | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._last_rx = 0.0
        self._handlers: DefaultDict[EventType, list[EventHandler]] = defaultdict(list)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, name="sync-manager", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=8.0):
            raise SyncError("同步线程启动超时")

    def stop(self) -> None:
        if not self._loop:
            return
        if self._stop_evt:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._async_stop(), self._loop
                ).result(timeout=3)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=6.0)
        self._loop = None
        self._thread = None
        # 支持 stop → start 重启：清掉就绪事件，让下次 start 真正等新线程就绪
        # （否则 _ready 保持置位，start 立即返回，后续 _submit 读到 _loop=None 报未启动）。
        self._ready.clear()

    async def _async_stop(self) -> None:
        if self._stop_evt:
            self._stop_evt.set()

    def _run_loop(self) -> None:
        asyncio.run(self._async_main())

    async def _async_main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_evt = asyncio.Event()
        self._cfg.ensure_data_dir()
        self._key_store = KeyStore(self._cfg.data_dir)
        self._get_identity()
        # 收编（S1/S2）：旧 sync_queue.db 迁入 core.db，再以 core.db 驱动队列/序号
        migrate_legacy_queue(self._db, self._cfg.queue_db_path, self._my_peer_id)
        self._queue = SyncQueue(self._db, self._my_peer_id)

        self._load_peer_record()
        self._transport = LanTransport(self._cfg, self._my_peer_id)
        self._transport.set_receiver(self._on_envelope)
        self._transport.set_state_cb(self._on_transport_state)
        await self._transport.start()
        if self._peer_id:
            await self._transport.set_peer(self._peer_id)

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._ready.set()
        try:
            await self._stop_evt.wait()
        finally:
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
            await self._transport.stop()
            self._queue.close()

    # ------------------------------------------------------------------ #
    # 查询（线程安全：仅读 loop 线程写入的字段，不跨线程写）
    # ------------------------------------------------------------------ #

    def pairing_status(self) -> PairingStatus:
        return PairingStatus.PAIRED if self._peer_id else PairingStatus.UNPAIRED

    def connection_status(self) -> ConnState:
        if self._transport is None:
            return ConnState.DISCONNECTED
        return self._transport.current_state()

    def peer_id(self) -> str | None:
        return self._my_peer_id or None

    def partner_id(self) -> str | None:
        return self._peer_id

    # ------------------------------------------------------------------ #
    # 业务接口（同步包装，投递到 loop 线程）
    # ------------------------------------------------------------------ #

    def add_handler(self, type: EventType, handler: EventHandler) -> None:
        self._handlers[type].append(handler)

    def send(
        self, type: EventType | str, payload: dict, *, expires_at: int | None = None
    ) -> None:
        self._submit(self._send_async(type, payload, expires_at=expires_at), timeout=8.0)

    def start_pairing(self) -> str:
        return self._submit(self._start_pairing_async(), timeout=15.0)

    def confirm_pairing(self, code: str) -> None:
        self._submit(self._confirm_pairing_async(code), timeout=20.0)

    def confirm_peer(self, peer_id: str) -> None:
        self._submit(self._confirm_peer_async(peer_id), timeout=15.0)

    def reject_peer(self, peer_id: str) -> None:
        self._submit(self._reject_peer_async(), timeout=10.0)

    def revoke_pairing(self) -> None:
        self._submit(self._revoke_async(), timeout=8.0)

    # ------------------------------------------------------------------ #
    # 内部：asyncio 实现
    # ------------------------------------------------------------------ #

    def _submit(self, coro: Coroutine, *, timeout: float):
        if not self._loop:
            raise SyncError("同步管理器未启动")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=timeout)
        except (PairingError, SyncError) as exc:
            raise exc
        except Exception as exc:  # noqa: BLE001
            raise SyncError(str(exc)) from exc

    # -- 身份与持久化 -- #

    def _get_identity(self) -> IdentityKeyPair:
        if self._identity is None:
            raw = self._key_store.load_identity()
            if raw:
                priv = PrivateKey(raw)
                self._identity = IdentityKeyPair(bytes(priv), bytes(priv.public_key))
            else:
                self._identity = generate_identity()
                self._key_store.save_identity(self._identity.private_key)
            self._my_peer_id = derive_peer_id(self._identity.public_key)
        return self._identity

    def _load_peer_record(self) -> None:
        peers_dir = Path(self._cfg.data_dir) / "peers"
        if not peers_dir.exists():
            return
        for p in sorted(peers_dir.glob("*.json")):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
                keys = self._key_store.load_peer_session(rec["peer_id"])
            except Exception:
                p.unlink(missing_ok=True)
                continue
            if keys is None:
                log.warning("伴侣会话密钥缺失，删除损坏记录 %s", p.name)
                p.unlink(missing_ok=True)
                continue
            self._peer_id = rec["peer_id"]
            self._pairing_version = int(rec["pairing_version"])
            self._session_keys = keys
            log.info("恢复配对：%s", self._peer_id)
            return

    def _write_peer_record(self, result: PairingResult) -> None:
        peers_dir = Path(self._cfg.data_dir) / "peers"
        peers_dir.mkdir(parents=True, exist_ok=True)
        path = peers_dir / f"{result.peer_id}.json"
        path.write_text(
            json.dumps(
                {
                    "peer_id": result.peer_id,
                    "pairing_version": result.pairing_version,
                    "paired_at": int(time.time()),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _clear_pairing(self) -> None:
        if self._peer_id:
            self._key_store.delete_peer(self._peer_id)
            (Path(self._cfg.data_dir) / "peers" / f"{self._peer_id}.json").unlink(
                missing_ok=True
            )
        self._peer_id = None
        self._peer_public = None
        self._session_keys = None
        self._pairing_version = None

    # -- 配对 -- #

    async def _start_pairing_async(self) -> str:
        if self._peer_id:
            raise PairingError("已配对，请先解除配对")
        identity = self._get_identity()
        self._pairing_session = PairingSession(
            self._cfg, identity, self._my_peer_id, self._on_pair_request
        )
        return await self._pairing_session.start()

    def _on_pair_request(self, peer_id: str, public_key: bytes) -> None:
        """loop 线程内回调（UI 确认后调 confirm_peer / reject_peer）。"""
        self._peer_public = public_key
        self._on_pairing(PairingEvent.PEER_REQUEST, {"peer_id": peer_id})

    async def _confirm_peer_async(self, peer_id: str) -> None:
        if not self._pairing_session:
            raise PairingError("无配对会话")
        result = await self._pairing_session.confirm()
        await self._apply_pairing(result)

    async def _reject_peer_async(self) -> None:
        if self._pairing_session:
            await self._pairing_session.reject()
            self._pairing_session = None

    async def _confirm_pairing_async(self, code: str) -> None:
        if self._peer_id:
            raise PairingError("已配对，请先解除配对")
        identity = self._get_identity()
        client = PairingClient(self._cfg, identity, self._my_peer_id)
        result = await client.join(code)
        await self._apply_pairing(result)

    async def _apply_pairing(self, result: PairingResult) -> None:
        self._key_store.save_peer_session(result.peer_id, result.session_keys)
        self._write_peer_record(result)
        self._peer_id = result.peer_id
        self._peer_public = result.peer_public
        self._session_keys = result.session_keys
        self._pairing_version = result.pairing_version
        self._pairing_session = None
        await self._transport.set_peer(result.peer_id)
        # 若配对完成前对方已连上（状态已是 CONNECTED，set_peer 不会重新触发回调），
        # 需手动补一次 _on_connected：发 hello + flush 本地 outbox。
        if self._transport.current_state() == ConnState.CONNECTED:
            await self._on_connected()
        self._on_pairing(PairingEvent.PAIRED, {"peer_id": result.peer_id})

    async def _revoke_async(self) -> None:
        if not self._peer_id:
            return
        try:
            await self._send_async(EventType.PAIRING_REVOKE, {}, expires_at=None)
        except Exception:
            pass
        await self._transport.set_peer(None)
        await self._transport.disconnect_connection()
        self._clear_pairing()
        self._on_pairing(PairingEvent.REVOKED, {"reason": "local_revoke"})

    # -- 发送 -- #

    async def _send_async(
        self, type: EventType | str, payload: dict, *, expires_at: int | None = None
    ) -> None:
        if self._peer_id is None:
            raise PairingError("未配对，禁止外发")
        type_str = type.value if isinstance(type, EventType) else str(type)
        seq = self._queue.make_seq_manager(self._peer_id).next()

        if (
            self._transport is not None
            and self._transport.current_state() == ConnState.CONNECTED
        ):
            msg = Message(
                v=PROTOCOL_VERSION,
                type=type_str,
                from_id=self._my_peer_id,
                seq=seq,
                ts=int(time.time()),
                payload=payload,
            )
            env = Envelope(
                to=self._peer_id,
                cipher=encrypt(self._session_keys.key_to_peer, message_to_json(msg)),
                expires=expires_at or 0,
            )
            try:
                await self._transport.send(self._peer_id, env)
                # 在线直发成功也落 events 表（status='sent'，D17）：events 表为
                # 完整双向事件日志，双端重放同一事件集收敛（spec §3.3.6）。
                self._queue.record_sent(
                    self._peer_id, seq, type_str, payload, expires_at
                )
                return
            except Exception as exc:
                log.debug("直发失败，转入离线队列: %s", exc)
        self._queue.enqueue(self._peer_id, seq, type_str, payload, expires_at)

    async def _flush_outbox(self) -> None:
        if not self._peer_id or not self._session_keys:
            return
        errors = await self._queue.flush(self._peer_id, self._send_queued_message)
        for exc in errors:
            log.debug("补发保留 pending: %s", exc)

    async def _send_queued_message(self, msg: Message) -> None:
        env = Envelope(
            to=self._peer_id,
            cipher=encrypt(self._session_keys.key_to_peer, message_to_json(msg)),
            expires=0,
        )
        await self._transport.send(self._peer_id, env)

    # -- 接收 -- #

    def _on_envelope(self, env) -> None:
        """loop 线程回调（transport 线程内）。"""
        if env.expires and env.expires < int(time.time()):
            return
        if self._session_keys is None:
            return
        try:
            plain = decrypt(self._session_keys.key_from_peer, env.cipher)
            msg = message_from_json(plain)
        except (CryptoError, ProtocolError) as exc:
            log.warning("解密/协议失败，断开连接: %s", exc)
            asyncio.ensure_future(self._transport.disconnect_connection())
            return
        self._last_rx = time.time()
        if msg.type == EventType.HELLO.value:
            asyncio.ensure_future(self._on_hello(msg, ack=False))
        elif msg.type == EventType.HELLO_ACK.value:
            asyncio.ensure_future(self._on_hello(msg, ack=True))
        elif msg.type == EventType.PAIRING_REVOKE.value:
            asyncio.ensure_future(self._on_revoke_received())
        else:
            self._queue.receive(msg, self._deliver)

    def _deliver(self, msg: Message) -> None:
        for handler in self._handlers.get(EventType(msg.type), []):
            handler(msg)
        self._on_event(msg)

    async def _on_hello(self, msg: Message, *, ack: bool) -> None:
        await self._check_pairing_version(msg.payload)
        if not ack:
            await self._send_hello(is_ack=True)

    async def _on_revoke_received(self) -> None:
        log.warning("收到对方吊销配对")
        await self._transport.set_peer(None)
        await self._transport.disconnect_connection()
        self._clear_pairing()
        self._on_pairing(PairingEvent.REVOKE_RECEIVED, {})

    async def _check_pairing_version(self, payload: dict) -> None:
        peer_v = payload.get("pairing_version")
        if (
            peer_v is not None
            and self._pairing_version is not None
            and peer_v != self._pairing_version
        ):
            log.warning("配对版本不一致(%s!=%s)，判定已吊销", peer_v, self._pairing_version)
            self._clear_pairing()
            await self._transport.set_peer(None)
            await self._transport.disconnect_connection()
            self._on_pairing(PairingEvent.REVOKED, {"reason": "version_mismatch"})

    # -- 心跳 -- #

    def _on_transport_state(self, state: ConnState) -> None:
        if state == ConnState.CONNECTED:
            asyncio.ensure_future(self._on_connected())
        self._on_state(state)

    async def _on_connected(self) -> None:
        # 未配对时（配对完成前对方抢先连接）直接早退，
        # 避免 _send_hello → _send_async 抛 PairingError 任务静默死亡。
        if not self._peer_id:
            return
        self._last_rx = time.time()
        await self._send_hello(is_ack=False)
        await self._flush_outbox()

    async def _send_hello(self, *, is_ack: bool) -> None:
        if not self._peer_id:
            return
        payload = {
            "pairing_version": self._pairing_version or 0,
            "device": platform.system(),
            "instance_id": self._cfg.instance_id,
        }
        await self._send_async(EventType.HELLO_ACK if is_ack else EventType.HELLO, payload)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cfg.heartbeat_interval)
            if (
                self._transport is not None
                and self._transport.current_state() == ConnState.CONNECTED
            ):
                await self._send_hello(is_ack=False)
                if time.time() - self._last_rx > (
                    self._cfg.heartbeat_interval * self._cfg.heartbeat_misses
                ):
                    log.warning("心跳超时，判定离线")
                    await self._transport.disconnect_connection()
