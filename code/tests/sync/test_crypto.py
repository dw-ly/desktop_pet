"""密码学封装测试（impl §14.1）。"""

import os

import pytest

from sync.crypto import (
    compute_sas,
    decode_pairing_code,
    derive_mac_key,
    derive_peer_id,
    derive_session,
    encrypt,
    decrypt,
    format_code,
    generate_identity,
    generate_pairing_code,
    random_session_id,
)
from sync.constants import CODE_CHARSET
from sync.errors import CryptoError, PairingError


def test_encrypt_decrypt_roundtrip() -> None:
    key = bytes(range(32))
    ct = encrypt(key, b"hello")
    assert decrypt(key, ct) == b"hello"


def test_tampered_cipher_fails() -> None:
    key = bytes(range(32))
    ct = bytearray(encrypt(key, b"hello"))
    ct[len(ct) // 2] ^= 0x01
    with pytest.raises(CryptoError):
        decrypt(key, bytes(ct))


def test_wrong_key_fails() -> None:
    ct = encrypt(bytes(32), b"hello")
    with pytest.raises(CryptoError):
        decrypt(os.urandom(32), ct)


def test_bidirectional_keys_separated() -> None:
    a = generate_identity()
    b = generate_identity()
    ka = derive_session(a.private_key, b.public_key)
    kb = derive_session(b.private_key, a.public_key)
    assert ka.key_to_peer == kb.key_from_peer
    assert ka.key_from_peer == kb.key_to_peer
    assert ka.key_to_peer != ka.key_from_peer


def test_public_key_serialization_stable() -> None:
    a = generate_identity()
    b = generate_identity()
    # 同一私钥导出的公钥字节一致（跨进程交换可复现）
    from nacl.public import PrivateKey

    priv = PrivateKey(a.private_key)
    assert bytes(priv.public_key) == a.public_key
    assert len(b.public_key) == 32


def test_sas_consistent() -> None:
    a = generate_identity()
    b = generate_identity()
    from sync.crypto import _shared_secret

    shared_a = _shared_secret(a.private_key, b.public_key)
    shared_b = _shared_secret(b.private_key, a.public_key)
    sas_a = compute_sas(shared_a, a.public_key, b.public_key)
    sas_b = compute_sas(shared_b, a.public_key, b.public_key)
    assert sas_a == sas_b
    assert len(sas_a) == 4


def test_sas_differs_with_other_party() -> None:
    a = generate_identity()
    b = generate_identity()
    c = generate_identity()
    from sync.crypto import _shared_secret

    shared = _shared_secret(a.private_key, b.public_key)
    sas1 = compute_sas(shared, a.public_key, b.public_key)
    sas2 = compute_sas(shared, a.public_key, c.public_key)
    assert sas1 != sas2


def test_pairing_code_roundtrip() -> None:
    sid = random_session_id()
    code = generate_pairing_code(sid)
    assert decode_pairing_code(code) == sid


def test_pairing_code_typo_detected() -> None:
    sid = random_session_id()
    code = generate_pairing_code(sid)
    # 翻转第 6 个字符（改为另一合法字符）
    ch = CODE_CHARSET[(CODE_CHARSET.index(code[5]) + 1) % len(CODE_CHARSET)]
    bad = code[:5] + ch + code[6:]
    with pytest.raises(PairingError):
        decode_pairing_code(bad)


def test_pairing_code_charset() -> None:
    sid = random_session_id()
    code = generate_pairing_code(sid)
    assert len(code) == 12
    assert all(c in CODE_CHARSET for c in code)
    assert format_code(code).count("-") == 2


def test_pairing_code_invalid_length() -> None:
    with pytest.raises(PairingError):
        decode_pairing_code("ABCDEFGHIJKLMNO")


def test_mac_key_independent() -> None:
    a = generate_identity()
    b = generate_identity()
    keys = derive_session(a.private_key, b.public_key)
    mac = derive_mac_key(a.private_key, b.public_key)
    assert mac != keys.key_to_peer
    assert mac != keys.key_from_peer
    assert len(mac) == 32


def test_derive_peer_id_stable() -> None:
    a = generate_identity()
    assert len(derive_peer_id(a.public_key)) == 16
    assert derive_peer_id(a.public_key) == derive_peer_id(a.public_key)
    b = generate_identity()
    assert derive_peer_id(a.public_key) != derive_peer_id(b.public_key)
