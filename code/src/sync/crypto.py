"""密码学封装（对应 impl §5 / plan S1）。

- X25519 密钥对生成与共享秘密派生
- blake2b 子键派生（libsodium crypto_kdf 兼容实现）分离双向会话密钥与 MAC 键
- XSalsa20-Poly1305（SecretBox）认证加密
- SAS 一次性验证码（防中间人）
- 12 位配对短码（含校验位）

对外暴露核心方法，不暴露裸共享秘密。
"""

from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass

from nacl import bindings
from nacl.public import PrivateKey, PublicKey, Box
from nacl.secret import SecretBox
from nacl.utils import random

from .constants import CODE_CHARSET, CODE_LEN, KD_CONTEXT, SUBKEY_ENC_HIGHER_TO_LOWER, SUBKEY_ENC_LOWER_TO_HIGHER, SUBKEY_MAC
from .errors import CryptoError, PairingError


@dataclass(frozen=True)
class IdentityKeyPair:
    private_key: bytes  # 32B
    public_key: bytes   # 32B


@dataclass(frozen=True)
class SessionKeys:
    key_to_peer: bytes    # 32B 本端→对方
    key_from_peer: bytes  # 32B 对方→本端


# --------------------------------------------------------------------------- #
# 基础编码（5-bit base32，按 impl §5.4）
# --------------------------------------------------------------------------- #

def _b32_encode_bits(data: bytes, bit_len: int) -> str:
    """把 data 视作大端位流，取前 bit_len 位（须为 5 的倍数）编码为 base32 字符串。"""
    bits = _bits_of(data)
    if bit_len > len(bits) or bit_len % 5 != 0:
        raise ValueError("bit_len 超出数据范围或不是 5 的倍数")
    out = []
    for i in range(0, bit_len, 5):
        chunk = 0
        for j in range(5):
            chunk = (chunk << 1) | bits[i + j]
        out.append(CODE_CHARSET[chunk])
    return "".join(out)


def _b32_decode_bits(s: str, bit_len: int) -> bytes:
    """把 bit_len 位（须为 5 的倍数）的 base32 字符串解码为字节（尾部补零）。"""
    if len(s) * 5 != bit_len:
        raise PairingError("短码长度非法")
    bits: list[int] = []
    for ch in s.upper():
        if ch not in CODE_CHARSET:
            raise PairingError(f"短码含非法字符: {ch!r}")
        v = CODE_CHARSET.index(ch)
        for i in range(4, -1, -1):
            bits.append((v >> i) & 1)
    byte_len = (bit_len + 7) // 8
    out = bytearray(byte_len)
    for i, bit in enumerate(bits):
        if bit:
            out[i // 8] |= 1 << (7 - (i % 8))
    return bytes(out)


def _bits_of(data: bytes) -> list[int]:
    bits: list[int] = []
    for b in data:
        for i in range(7, -1, -1):
            bits.append((b >> i) & 1)
    return bits


# --------------------------------------------------------------------------- #
# 身份与会话密钥
# --------------------------------------------------------------------------- #

def generate_identity() -> IdentityKeyPair:
    priv = PrivateKey.generate()
    return IdentityKeyPair(
        private_key=bytes(priv),
        public_key=bytes(priv.public_key),
    )


def _shared_secret(my_private: bytes, peer_public: bytes) -> bytes:
    """X25519 共享秘密（32B）。不缓存、不入日志。"""
    return bindings.crypto_box_beforenm(peer_public, my_private)


def _kdf_derive(subkey_id: int, context: bytes, master: bytes, size: int = 32) -> bytes:
    """libsodium crypto_kdf 兼容的子键派生（PyNaCl 未绑定该函数，用 blake2b 等价实现）。

    构造：BLAKE2b(key=master) 的输入为 `subkey_id(LE64) || context`，
    与 libsodium `crypto_kdf_derive_from_key` 输出字节一致。
    同一 master 下，不同 subkey_id 派生出的子键相互独立。
    """
    h = hashlib.blake2b(key=master, digest_size=size)
    h.update(struct.pack("<Q", subkey_id))
    h.update(context)
    return h.digest()


def derive_session(my_private: bytes, peer_public: bytes) -> SessionKeys:
    """共享秘密 → 分离双向会话密钥（impl §5.1）。

    方向键按「两端公钥的规范序」分配（A.pub < B.pub 则子键 1 为 A→B 方向、
    子键 2 为 B→A 方向），双方各自计算得到相同结论，从而
    A.key_to_peer == B.key_from_peer、A.key_from_peer == B.key_to_peer。
    """
    shared = _shared_secret(my_private, peer_public)
    my_public = bytes(PrivateKey(my_private).public_key)
    try:
        if my_public < peer_public:
            key_to_peer = _kdf_derive(SUBKEY_ENC_LOWER_TO_HIGHER, KD_CONTEXT, shared)
            key_from_peer = _kdf_derive(SUBKEY_ENC_HIGHER_TO_LOWER, KD_CONTEXT, shared)
        else:
            key_to_peer = _kdf_derive(SUBKEY_ENC_HIGHER_TO_LOWER, KD_CONTEXT, shared)
            key_from_peer = _kdf_derive(SUBKEY_ENC_LOWER_TO_HIGHER, KD_CONTEXT, shared)
    except Exception as exc:
        raise CryptoError(f"会话密钥派生失败: {exc}") from exc
    return SessionKeys(key_to_peer=key_to_peer, key_from_peer=key_from_peer)


def derive_mac_key(my_private: bytes, peer_public: bytes) -> bytes:
    """HMAC 签名键（impl §5.1，data-consistency pet.feed 消费）。"""
    shared = _shared_secret(my_private, peer_public)
    try:
        return _kdf_derive(SUBKEY_MAC, KD_CONTEXT, shared)
    except Exception as exc:
        raise CryptoError(f"MAC 键派生失败: {exc}") from exc


# --------------------------------------------------------------------------- #
# 加解密
# --------------------------------------------------------------------------- #

def encrypt(key: bytes, plaintext: bytes) -> bytes:
    """SecretBox 认证加密，返回 nonce||密文。"""
    return SecretBox(key).encrypt(plaintext)


def decrypt(key: bytes, ciphertext: bytes) -> bytes:
    """解密并验证认证标签；篡改/错键抛 CryptoError。"""
    try:
        return SecretBox(key).decrypt(ciphertext)
    except Exception as exc:
        raise CryptoError(f"解密失败: {exc}") from exc


# --------------------------------------------------------------------------- #
# peer_id 派生（impl §1.2）
# --------------------------------------------------------------------------- #

def derive_peer_id(public_key: bytes) -> str:
    """base32(blake2b(pub, 10B)) → 16 字符大写 base32。"""
    digest = hashlib.blake2b(public_key, digest_size=10).digest()
    return base64.b32encode(digest).decode("ascii").rstrip("=")


# --------------------------------------------------------------------------- #
# SAS 一次性验证码（impl §5.3）
# --------------------------------------------------------------------------- #

def compute_sas(shared: bytes, pub_a: bytes, pub_b: bytes) -> str:
    """双方用相同输入计算，结果必须一致；4 字符 base32。"""
    digest = hashlib.blake2b(shared + pub_a + pub_b, digest_size=3).digest()
    return _b32_encode_bits(digest, 20)


# --------------------------------------------------------------------------- #
# 配对短码（impl §5.4）
# --------------------------------------------------------------------------- #

_SESSION_BITS = (CODE_LEN - 4) * 5          # 8 字符 = 40 bit = 5 字节
_CHECKSUM_BITS = 4 * 5                       # 4 字符 = 20 bit


def generate_pairing_code(session_id: bytes) -> str:
    """session_id: 5B(40bit) CSPRNG → 12 位短码（8 位会话 + 4 位校验）。"""
    if len(session_id) != _SESSION_BITS // 8:
        raise ValueError("session_id 必须为 5 字节")
    checksum = hashlib.blake2b(session_id, digest_size=3).digest()
    code = _b32_encode_bits(session_id, _SESSION_BITS) + _b32_encode_bits(checksum, _CHECKSUM_BITS)
    assert len(code) == CODE_LEN
    return code


def format_code(code: str) -> str:
    """显示分组 XXXX-XXXX-XXXX。"""
    return f"{code[0:4]}-{code[4:8]}-{code[8:12]}"


def decode_pairing_code(code: str) -> bytes:
    """去 '-' 校验 12 位与字符集；校验和不符抛 PairingError；返回 session_id。"""
    cleaned = code.replace("-", "").strip().upper()
    if len(cleaned) != CODE_LEN:
        raise PairingError("短码长度必须为 12 位")
    if any(ch not in CODE_CHARSET for ch in cleaned):
        raise PairingError("短码含非法字符")
    session_part = cleaned[:8]
    checksum_part = cleaned[8:]
    session_id = _b32_decode_bits(session_part, _SESSION_BITS)
    expect = _b32_encode_bits(
        hashlib.blake2b(session_id, digest_size=3).digest(), _CHECKSUM_BITS
    )
    if expect != checksum_part:
        raise PairingError("短码校验失败，请核对输入")
    return session_id


def random_session_id() -> bytes:
    return random(_SESSION_BITS // 8)
