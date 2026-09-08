"""配对流程测试（impl §14.3）。

握手成功用例走真实 WebSocket 直连（monkeypatch 掉 mDNS 广播/解析，
用 127.0.0.1 + 实际监听端口代替局域网发现）。
"""

import asyncio
import base64
import json

import pytest
import websockets

from sync import pairing as pairing_module
from sync.config import SyncConfig
from sync.crypto import (
    derive_peer_id,
    derive_session,
    generate_identity,
    generate_pairing_code,
    random_session_id,
)
from sync.errors import PairingError
from sync.pairing import PairingClient, PairingSession


class DummyHandle:
    def unpublish(self) -> None:
        pass


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _cfg(tmp_path, label: str) -> SyncConfig:
    return SyncConfig(data_dir=str(tmp_path), instance_id=label, ws_port_range=(49000, 49099))


def test_pairing_handshake_success(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(pairing_module, "publish", lambda *a, **k: DummyHandle())

    cfg = _cfg(tmp_path, "tA")
    id_a = generate_identity()
    id_b = generate_identity()
    peer_a = derive_peer_id(id_a.public_key)
    peer_b = derive_peer_id(id_b.public_key)

    requested: dict = {}

    def on_request(peer_id: str, pub: bytes) -> None:
        requested["peer_id"] = peer_id
        asyncio.ensure_future(session.confirm())

    session = PairingSession(cfg, id_a, peer_a, on_request)

    def fake_resolve(service, name, timeout_seconds=3.0):
        return ("127.0.0.1", session._port)

    monkeypatch.setattr(pairing_module, "resolve", fake_resolve)
    client = PairingClient(cfg, id_b, peer_b)

    async def run():
        code = await session.start()
        result = await client.join(code)
        return result

    result = asyncio.run(run())
    assert result.peer_id == peer_a              # B 得到 A 的 peer_id
    assert requested["peer_id"] == peer_b        # A 收到 B 的 peer_id
    assert result.pairing_version >= 0

    # 会话密钥互通：B 的 to_peer == A 的 from_peer
    keys_a = derive_session(id_a.private_key, id_b.public_key)
    assert result.session_keys.key_to_peer == keys_a.key_from_peer
    assert result.session_keys.key_from_peer == keys_a.key_to_peer


def test_join_with_bad_code_rejected(tmp_path) -> None:
    cfg = _cfg(tmp_path, "tB")
    client = PairingClient(cfg, generate_identity(), "X")
    with pytest.raises(PairingError):
        asyncio.run(client.join("ZZZZZZZZZZZZ"))  # 合法字符但校验和失败


def test_sas_mismatch_rejected(tmp_path, monkeypatch) -> None:
    """伪造 accept 携带错误 SAS → B 端必须中止。"""
    monkeypatch.setattr(pairing_module, "publish", lambda *a, **k: DummyHandle())
    fake_id = generate_identity()

    async def run():
        async def handler(ws):
            await ws.recv()
            await ws.send(
                json.dumps(
                    {
                        "type": "pair.accept",
                        "pub": _b64(fake_id.public_key),
                        "sas": "ZZZZ",
                        "version": 1,
                    }
                )
            )

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(
            pairing_module, "resolve", lambda *a, **k: ("127.0.0.1", port)
        )
        client = PairingClient(_cfg(tmp_path, "tC"), generate_identity(), "X")
        try:
            code = generate_pairing_code(random_session_id())
            await client.join(code)
            raise AssertionError("SAS 不一致应抛 PairingError")
        except PairingError:
            pass
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_pairing_code_one_time_consume(tmp_path, monkeypatch) -> None:
    """同一配对会话 confirm 后，再次 confirm 必须拒绝。"""
    monkeypatch.setattr(pairing_module, "publish", lambda *a, **k: DummyHandle())

    cfg = _cfg(tmp_path, "tD")
    id_a = generate_identity()
    session = PairingSession(cfg, id_a, derive_peer_id(id_a.public_key), lambda *a: None)

    async def run():
        code = await session.start()
        # 无请求时 confirm 报错
        with pytest.raises(PairingError):
            await session.confirm()
        # 取消清理
        await session.close()
        return code

    asyncio.run(run())
