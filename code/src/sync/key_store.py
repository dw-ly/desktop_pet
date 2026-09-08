"""密钥存储（对应 impl §6 / plan S2）。

优先 keyring（Windows DPAPI / Linux SecretService）；不可用时降级为受限权限文件（0600）。
私钥与会话密钥永不明文外泄。
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

import keyring

from .crypto import SessionKeys
from .errors import KeyStoreError

log = logging.getLogger(__name__)

_SERVICE = "tuanzi-pet"
_KEYRING_BACKENDS = None  # 探测标记


def _keyring_available() -> bool:
    """探测 keyring 是否有可用后端（结果缓存）。"""
    global _KEYRING_BACKENDS
    if _KEYRING_BACKENDS is None:
        try:
            _KEYRING_BACKENDS = bool(keyring.backends.get_all_keyring())
        except Exception:
            _KEYRING_BACKENDS = False
    return _KEYRING_BACKENDS


class KeyStore:
    """密钥加密落盘：keyring 优先，文件（0600）兜底。"""

    def __init__(self, data_dir: str) -> None:
        self._keys_dir = Path(data_dir) / "keys"

    # ------------------------------------------------------------------ #
    # 底层存取
    # ------------------------------------------------------------------ #

    def _store(self, name: str, value: bytes) -> None:
        b64 = base64.b64encode(value).decode("ascii")
        if _keyring_available():
            try:
                keyring.set_password(_SERVICE, name, b64)
                return
            except Exception as exc:  # 降级
                log.warning("keyring 写入失败(%s)，降级到受限权限文件", exc)
        self._write_file(name, b64)

    def _load(self, name: str) -> bytes | None:
        if _keyring_available():
            try:
                v = keyring.get_password(_SERVICE, name)
                if v:
                    return base64.b64decode(v)
            except Exception as exc:
                log.warning("keyring 读取失败(%s)，尝试文件兜底", exc)
        path = self._keys_dir / f"{name}.key"
        if path.exists():
            return base64.b64decode(path.read_text(encoding="ascii").strip())
        return None

    def _delete(self, name: str) -> bool:
        deleted = False
        if _keyring_available():
            try:
                keyring.delete_password(_SERVICE, name)
                deleted = True
            except Exception:
                pass
        path = self._keys_dir / f"{name}.key"
        if path.exists():
            path.unlink()
            deleted = True
        return deleted

    def _write_file(self, name: str, b64: str) -> None:
        try:
            self._keys_dir.mkdir(parents=True, exist_ok=True)
            path = self._keys_dir / f"{name}.key"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(b64)
            os.chmod(path, 0o600)
        except OSError as exc:
            raise KeyStoreError(f"密钥文件写入失败: {exc}") from exc

    # ------------------------------------------------------------------ #
    # 身份密钥
    # ------------------------------------------------------------------ #

    def save_identity(self, private_key: bytes) -> None:
        self._store("identity", private_key)

    def load_identity(self) -> bytes | None:
        return self._load("identity")

    def delete_identity(self) -> bool:
        return self._delete("identity")

    # ------------------------------------------------------------------ #
    # 伴侣会话密钥
    # ------------------------------------------------------------------ #

    @staticmethod
    def _pack_session(keys: SessionKeys) -> bytes:
        return keys.key_to_peer + keys.key_from_peer

    @staticmethod
    def _unpack_session(raw: bytes) -> SessionKeys:
        if len(raw) != 64:
            raise KeyStoreError("会话密钥数据长度非法")
        return SessionKeys(key_to_peer=raw[:32], key_from_peer=raw[32:])

    def save_peer_session(self, peer_id: str, session_keys: SessionKeys) -> None:
        self._store(f"peer:{peer_id}", self._pack_session(session_keys))

    def load_peer_session(self, peer_id: str) -> SessionKeys | None:
        raw = self._load(f"peer:{peer_id}")
        return self._unpack_session(raw) if raw else None

    def delete_peer(self, peer_id: str) -> bool:
        return self._delete(f"peer:{peer_id}")
