"""事件类型与数据结构测试（impl §14.2）。"""

from sync.constants import PROTOCOL_VERSION
from sync.errors import ProtocolError
from sync.events import (
    BUSINESS_TYPES,
    INTERNAL_TYPES,
    EventType,
    Envelope,
    Message,
    envelope_from_json,
    envelope_to_json,
    message_from_json,
    message_to_json,
)

ALL_TYPES = [
    "hello", "hello.ack", "pairing.revoke",
    "msg.carry", "carry.ack", "carry.revoke",
    "mood.sync", "pet.feed", "pet.profile",
    "date.add", "date.remind", "date.sync",
    "gift.send", "gift.accept", "gift.expire",
]

_TYPICAL_PAYLOAD = {
    "hello": {"pairing_version": 1, "device": "win11", "instance_id": "X"},
    "hello.ack": {"pairing_version": 1, "device": "win11", "instance_id": "X"},
    "pairing.revoke": {},
    "msg.carry": {"text": "hi", "carryId": "c1", "expireAt": 0},
    "carry.ack": {"carryId": "c1", "readAt": 0},
    "carry.revoke": {"carryId": "c1"},
    "mood.sync": {"valence": 0.6, "arousal": 0.2, "label": "sleepy"},
    "pet.feed": {"delta": 5, "reason": "feed", "ts": 0, "sig": "..."},
    "pet.profile": {"petName": "团子", "ts": 0},
    "date.add": {"date": "2026-08-03", "title": "纪念日", "blessing": "..."},
    "date.remind": {"date": "2026-08-03", "title": "纪念日", "blessing": "..."},
    "date.sync": {"streak": 3, "intimacy": 100, "ts": 0},
    "gift.send": {"giftId": "g1", "item": "rose", "to": "peer"},
    "gift.accept": {"giftId": "g1", "item": "rose"},
    "gift.expire": {"giftId": "g1"},
}


def _msg(t: str, seq: int = 1) -> Message:
    return Message(
        v=PROTOCOL_VERSION, type=t, from_id="A", seq=seq,
        ts=1754040000, payload=_TYPICAL_PAYLOAD[t],
    )


def test_event_type_registry_complete() -> None:
    assert {t.value for t in EventType} == set(ALL_TYPES)
    assert len(EventType) == 15


def test_internal_vs_business_disjoint() -> None:
    assert INTERNAL_TYPES & BUSINESS_TYPES == frozenset()
    assert len(INTERNAL_TYPES) == 3
    assert len(BUSINESS_TYPES) == 12


def test_message_roundtrip_all_types() -> None:
    for t in ALL_TYPES:
        m = _msg(t)
        assert message_from_json(message_to_json(m)) == m, t


def test_message_key_order_stable() -> None:
    raw = message_to_json(_msg("hello"))
    assert b'"v"' == raw[1:4]
    assert b'"type"' in raw[:30]
    assert b'"payload"' in raw


def test_envelope_roundtrip() -> None:
    e = Envelope(to="B", cipher=b"\x00\x01\x02\xff", expires=1754043600)
    assert envelope_from_json(envelope_to_json(e)) == e


def test_bad_json_raises_protocol_error() -> None:
    for raw in (b"{", b"not json", b"[]", b'"str"'):
        try:
            message_from_json(raw)
            assert False, f"应抛出 ProtocolError: {raw!r}"
        except ProtocolError:
            pass


def test_unknown_type_raises() -> None:
    raw = b'{"v":1,"type":"x.y","from":"A","seq":1,"ts":1,"payload":{}}'
    try:
        message_from_json(raw)
        assert False, "应抛出 ProtocolError"
    except ProtocolError:
        pass


def test_version_mismatch_raises() -> None:
    raw = b'{"v":2,"type":"hello","from":"A","seq":1,"ts":1,"payload":{}}'
    try:
        message_from_json(raw)
        assert False, "应抛出 ProtocolError"
    except ProtocolError:
        pass


def test_missing_fields_raises() -> None:
    raw = b'{"v":1,"type":"hello"}'
    try:
        message_from_json(raw)
        assert False, "应抛出 ProtocolError"
    except ProtocolError:
        pass


def test_bad_envelope_cipher_raises() -> None:
    raw = b'{"to":"B","cipher":"!!!","expires":0}'
    try:
        envelope_from_json(raw)
        assert False, "应抛出 ProtocolError"
    except ProtocolError:
        pass
