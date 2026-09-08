"""协议序号与去重键测试（impl §14.2）。"""

from sync.protocol import SeqManager, dedup_key, check_version
from sync.constants import PROTOCOL_VERSION
from sync.errors import ProtocolError


class FakeMeta:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value


def test_seq_manager_increments() -> None:
    meta = FakeMeta()
    mgr = SeqManager("A", meta.get, meta.set)
    assert mgr.next() == 1
    assert mgr.next() == 2
    assert mgr.next() == 3


def test_seq_manager_no_regression_after_restart() -> None:
    meta = FakeMeta()
    mgr = SeqManager("A", meta.get, meta.set)
    mgr.next()
    mgr.next()
    mgr.next()  # 已分配 1,2,3；游标已持久化为 4
    # 模拟重启：用同一 meta 重建 SeqManager
    mgr2 = SeqManager("A", meta.get, meta.set)
    nxt = mgr2.next()
    assert nxt >= 4


def test_seq_manager_per_peer_isolated() -> None:
    meta = FakeMeta()
    ma = SeqManager("A", meta.get, meta.set)
    mb = SeqManager("B", meta.get, meta.set)
    ma.next()
    assert mb.next() == 1  # B 的游标不受 A 影响


def test_dedup_key_unique_across_peers() -> None:
    assert dedup_key("A", 42) != dedup_key("B", 42)
    assert dedup_key("A", 42) == dedup_key("A", 42)


def test_check_version_ok() -> None:
    check_version(PROTOCOL_VERSION)


def test_check_version_rejects() -> None:
    try:
        check_version(PROTOCOL_VERSION + 1)
        assert False, "应抛出 ProtocolError"
    except ProtocolError:
        pass
