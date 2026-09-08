"""局域网直连传输（对应 impl §9 / plan S6）。

- Transport 抽象接口（中继实现 M3 预留）
- LanTransport：双端 WS 服务器 + 客户端、mDNS 发现、连接状态机、确定性单连接
- 心跳定时在 manager 侧（impl §10.4），transport 只负责连接与帧收发
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from enum import Enum

import websockets

from .config import SyncConfig
from .constants import (
    MDNS_SERVICE,
    RECONNECT_BACKOFF,
    WS_PORT_RANGE,
)
from .discovery import DiscoveryHandle, publish, resolve
from .errors import ProtocolError, TransportError
from .events import Envelope, envelope_from_json, envelope_to_json

log = logging.getLogger(__name__)


class ConnState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"


async def _serve_on_free_port(handler, port_range) -> tuple:
    """在端口范围内起一个 WS 服务，返回 (server, port)。"""
    for port in range(port_range[0], port_range[1] + 1):
        try:
            server = await websockets.serve(handler, host="", port=port)
            return server, port
        except OSError:
            continue
    raise TransportError(f"端口范围 {port_range} 内无可用端口")


class Transport(ABC):
    """传输抽象。中继实现（RelayTransport）在 M3 填充，接口保持稳定。"""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send(self, peer_id: str, envelope: Envelope) -> None: ...

    def set_receiver(self, cb) -> None: ...
    def set_state_cb(self, cb) -> None: ...

    @abstractmethod
    async def set_peer(self, peer_id: str | None) -> None: ...

    @abstractmethod
    async def disconnect_connection(self) -> None: ...


class RelayTransport(Transport):
    """中继传输（M3 启用）。当前仅占位保证接口稳定。"""

    async def start(self) -> None:
        raise NotImplementedError("中继传输在 M3 实现")

    async def stop(self) -> None:
        raise NotImplementedError

    async def send(self, peer_id: str, envelope: Envelope) -> None:
        raise NotImplementedError

    async def set_peer(self, peer_id: str | None) -> None:
        raise NotImplementedError

    async def disconnect_connection(self) -> None:
        raise NotImplementedError


class LanTransport(Transport):
    """局域网直连：双端互为 WS 服务器 + 客户端。

    确定性单连接：peer_id 字典序**小者**主动 connect，**大者**被动等待，
    避免双向同时建立两条连接。
    """

    def __init__(self, cfg: SyncConfig, my_peer_id: str) -> None:
        self._cfg = cfg
        self._my_peer_id = my_peer_id

        self._receiver = None      # Callable[[Envelope], None]
        self._state_cb = None      # Callable[[ConnState], None]
        self._state = ConnState.DISCONNECTED

        self._stopped = True
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._port: int | None = None
        self._handle: DiscoveryHandle | None = None
        self._ws = None
        self._peer_id: str | None = None   # 目标伴侣
        self._peer_event = asyncio.Event()
        # 连接断开通知：_recv_loop 结束时 set。被动方 runner 依赖它阻塞至连接断开。
        self._conn_lost = asyncio.Event()
        self._main_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopped = False
        self._server, self._port = await _serve_on_free_port(
            self._incoming_handler, self._cfg.ws_port_range or WS_PORT_RANGE
        )
        # zeroconf 为阻塞调用（内部自带事件循环），不能在 sync 的 asyncio loop 内同步执行，
        # 否则抛 EventLoopBlocked；用 to_thread 挪到工作线程。
        self._handle = await asyncio.to_thread(
            publish, MDNS_SERVICE, f"tuanzi-{self._my_peer_id}", self._port, {}
        )
        self._main_task = asyncio.create_task(self._runner())
        log.info("同步传输已启动，端口 %s", self._port)

    async def stop(self) -> None:
        self._stopped = True
        # 必须先捕获并关闭 ws：若先 cancel runner，CancelledError 会穿透
        # _recv_loop 的 finally 把 self._ws 清成 None，此处就关不到 → 对端
        # 收不到 CLOSE、recv_loop 永不退出，永远以为在线。
        ws = self._ws
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(), timeout=2.0)
            except Exception:
                pass
            self._ws = None
        if self._main_task:
            self._main_task.cancel()
            try:
                await self._main_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._handle:
            try:
                await asyncio.to_thread(self._handle.unpublish)
            except Exception:
                pass
            self._handle = None
        self._set_state(ConnState.DISCONNECTED)

    async def set_peer(self, peer_id: str | None) -> None:
        self._peer_id = peer_id
        if peer_id and not self._stopped:
            # 唤醒 runner 立刻评估是否连接
            self._peer_event.set()

    async def disconnect_connection(self) -> None:
        """由 manager 心跳超时调用：关闭当前连接触发重连。"""
        ws = self._ws
        if ws is not None:
            try:
                # 对端可能已失联（心跳超时场景），close 不能无限等 CLOSE ack
                await asyncio.wait_for(ws.close(), timeout=2.0)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # 收发
    # ------------------------------------------------------------------ #

    def set_receiver(self, cb) -> None:
        self._receiver = cb

    def set_state_cb(self, cb) -> None:
        self._state_cb = cb

    def current_state(self) -> ConnState:
        return self._state

    async def send(self, peer_id: str, envelope: Envelope) -> None:
        ws = self._ws
        if ws is None:
            raise TransportError("未连接，无法发送")
        try:
            await ws.send(envelope_to_json(envelope))
        except (websockets.ConnectionClosed, OSError, RuntimeError) as exc:
            raise TransportError(f"发送失败: {exc}") from exc

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _set_state(self, state: ConnState) -> None:
        if self._state != state:
            self._state = state
            if self._state_cb:
                self._state_cb(state)

    def _attach(self, ws) -> None:
        self._ws = ws
        self._conn_lost.clear()
        self._peer_event.set()
        self._set_state(ConnState.CONNECTED)

    async def _incoming_handler(self, ws) -> None:
        """被动方：收到客户端连接。"""
        self._attach(ws)
        await self._recv_loop(ws)

    async def _recv_loop(self, ws) -> None:
        try:
            async for raw in ws:
                try:
                    env = envelope_from_json(raw)
                except ProtocolError as exc:
                    log.debug("信封解析失败: %s", exc)
                    continue
                if self._receiver:
                    self._receiver(env)
        except websockets.ConnectionClosed:
            pass
        finally:
            if self._ws is ws:
                self._ws = None
                # 连接断开必须重置状态，否则 manager._on_transport_state 不再触发，
                # 重连后 _attach 的 CONNECTED 因状态未变被吞掉 → outbox 不 flush。
                self._set_state(ConnState.DISCONNECTED)
                # 通知被动方 runner：连接已断开，可进入重连循环。
                self._conn_lost.set()

    async def _runner(self) -> None:
        """连接主循环：主动方重连 / 被动方等待。"""
        while not self._stopped:
            if not self._peer_id:
                # 未配对，等待 set_peer 唤醒。wait_for 超时抛 TimeoutError，
                # 必须吞掉并继续轮询，否则 runner 任务崩溃 → 主动方永远不连接。
                self._peer_event.clear()
                try:
                    await asyncio.wait_for(self._peer_event.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                continue

            if self._ws is not None:
                # 已有活跃连接（被动方可能由 _incoming_handler 在配对完成前抢先 attach，
                # 而 runner 此时才刚得知 peer_id）。绝不能置 CONNECTING 覆盖 CONNECTED，
                # 否则状态抖动 → manager._on_connected 不再触发 → 在线/离线消息失灵。
                # 镜像主动方语义：阻塞至连接断开再进入重连。
                self._conn_lost.clear()
                await self._conn_lost.wait()
                if self._stopped:
                    break
                self._set_state(ConnState.RECONNECTING)
                await asyncio.sleep(0.3)
                continue

            is_active = self._my_peer_id < self._peer_id
            log.debug("运行器决策：is_active=%s my=%s peer=%s", is_active, self._my_peer_id, self._peer_id)
            self._set_state(ConnState.CONNECTING)

            if is_active:
                await self._connect_until_connected()
            else:
                await self._wait_for_peer()

            if self._stopped:
                break
            # 连接断开（recv_loop 返回后 ws 已清空）→ 重连
            self._set_state(ConnState.RECONNECTING)
            await asyncio.sleep(0.3)

    async def _connect_until_connected(self) -> None:
        """主动方：mDNS 解析 → 连接 → 接收循环直到断开。"""
        backoff_idx = 0
        while not self._stopped:
            addr = await asyncio.to_thread(
                resolve, MDNS_SERVICE, f"tuanzi-{self._peer_id}", 1.5
            )
            log.debug("解析伴侣 %s → %s", self._peer_id, addr)
            if addr:
                host, port = addr
                try:
                    ws = await websockets.connect(
                        f"ws://{host}:{port}", open_timeout=5.0
                    )
                    log.info("已连接对方 %s:%s", host, port)
                    self._attach(ws)
                    await self._recv_loop(ws)  # 阻塞至断开
                    return
                except (OSError, websockets.InvalidHandshake) as exc:
                    log.debug("连接失败: %s", exc)
            delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
            backoff_idx = min(backoff_idx + 1, len(RECONNECT_BACKOFF) - 1)
            await asyncio.sleep(delay)

    async def _wait_for_peer(self) -> None:
        """被动方：等待客户端连入，并阻塞至连接断开才返回。

        若连入后立即返回，runner 会立刻置 RECONNECTING 覆盖 _attach 的
        CONNECTED，导致状态在连接存活期间不断抖动 → manager._on_connected
        只在 DISCONNECTED→CONNECTED 时触发，状态抖动使离线补发/在线互发失灵。
        镜像主动方 _connect_until_connected 的"连接建立后阻塞至断开"语义。
        """
        self._peer_event.clear()
        try:
            await asyncio.wait_for(self._peer_event.wait(), timeout=None)
        except asyncio.TimeoutError:
            pass
        await self._conn_lost.wait()
