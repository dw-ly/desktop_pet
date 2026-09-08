"""带话双端联调脚本（对应 carry-message impl §7 / plan C3）。

复用 sync_demo 的双端骨架（临时 data_dir + discovery + 配对），扩展带话场景：
  配对 → 发送 → 播报/确认 → delivered → 撤回（B 即时隐藏）→ 过期失败。
全程断言：无重复播报、两端状态收敛一致。

用法（在 code 目录下）：
    .venv\\Scripts\\python tools\\carry_demo.py            # 常规（INFO）
    .venv\\Scripts\\python tools\\carry_demo.py --debug   # 含帧级日志

加密说明：消息经 sync 层 Envelope（密文 + expires）透传，demo 可见的均为解密后的
业务 Message；密文保证由 sync-security 单测与 sync_demo 覆盖，本脚本聚焦业务收敛。
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core import init_core  # noqa: E402
from core.carry import CarryConfig, CarryService  # noqa: E402
from core.carry_receive import CarryReceiver  # noqa: E402
from core.carry_store import CarryRecord, CarryStatus, CarryStore  # noqa: E402
from sync import EventType, SyncConfig, SyncManager  # noqa: E402
from sync.crypto import format_code  # noqa: E402
from sync.manager import PairingEvent  # noqa: E402

log = logging.getLogger("carry_demo")


class DemoNotify:
    """播报接口：记录 show/hide；保存 on_ack 供主线程手动触发（模拟点击"知道了"）。

    不自动回 ack——保证撤回分支的确定性（撤回前 B 端确认态可控）。
    """

    def __init__(self, label: str) -> None:
        self.label = label
        self.shown: dict[str, Callable[[], None]] = {}  # carry_id -> on_ack
        self.shown_count = 0
        self.hides: list[str] = []

    def show_carry(self, rec: CarryRecord, on_ack: Callable[[], None]) -> None:
        log.info("[%s] 播报: TA 说：%s", self.label, rec.text)
        self.shown[rec.id] = on_ack
        self.shown_count += 1

    def click_ack(self, carry_id: str) -> None:
        cb = self.shown.get(carry_id)
        if cb:
            cb()

    def hide_carry(self, carry_id: str) -> None:
        log.info("[%s] 即时隐藏: %s", self.label, carry_id)
        self.hides.append(carry_id)


def make_side(data_dir: str, label: str, port_range: tuple, on_pairing) -> SimpleNamespace:
    """一端完整装配：sync + core.db + store + service + receiver + handler 接线。"""
    cfg = SyncConfig(data_dir=data_dir, instance_id=label, ws_port_range=port_range)
    db = init_core(data_dir)
    mgr = SyncManager(
        cfg,
        db=db,
        on_state=lambda s: log.info("[%s] 连接状态=%s", label, s.value),
        on_pairing=on_pairing,
    )
    store = CarryStore(db)
    notify = DemoNotify(label)
    confirms: list[CarryRecord] = []
    statuses: list[CarryRecord] = []
    svc = CarryService(
        store, mgr, CarryConfig(),
        on_confirm=lambda r: confirms.append(r),
        on_status=lambda r: statuses.append(r),
        on_hide=notify.hide_carry,
    )
    recv = CarryReceiver(store, svc, notify, is_dnd=lambda: False)
    mgr.add_handler(EventType.MSG_CARRY, recv.on_msg)
    mgr.add_handler(EventType.CARRY_ACK, svc.on_ack)
    mgr.add_handler(EventType.CARRY_REVOKE, svc.on_revoke)
    return SimpleNamespace(
        mgr=mgr, db=db, store=store, svc=svc, recv=recv,
        notify=notify, confirms=confirms, statuses=statuses,
    )


def wait_until(pred, timeout: float, desc: str) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.1)
    raise RuntimeError(f"等待超时: {desc}")


def run_demo(port_range=(48000, 48099)) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tuanzi-carry-demo-"))
    dir_a = str(tmp / "a")
    dir_b = str(tmp / "b")

    auto_confirm = threading.Event()

    side_a = None
    side_b = None

    def on_pairing_a(e: PairingEvent, d: dict) -> None:
        log.info("[A] 配对事件=%s %s", e.value, d)
        if e == PairingEvent.PEER_REQUEST:
            peer_id = d["peer_id"]
            threading.Thread(
                target=lambda: side_a.mgr.confirm_peer(peer_id), daemon=True
            ).start()
        if e == PairingEvent.PAIRED:
            auto_confirm.set()

    def on_pairing_b(e: PairingEvent, d: dict) -> None:
        log.info("[B] 配对事件=%s %s", e.value, d)

    side_a = make_side(dir_a, "carryA", port_range, on_pairing_a)
    side_b = make_side(dir_b, "carryB", port_range, on_pairing_b)

    try:
        # 1. 启动 + 配对
        log.info("=== 启动双端 ===")
        side_a.mgr.start()
        side_b.mgr.start()
        log.info("=== 配对 ===")
        code = side_a.mgr.start_pairing()
        log.info("[A] 配对码: %s", format_code(code))
        side_b.mgr.confirm_pairing(code)
        wait_until(lambda: auto_confirm.is_set(), 15, "A 完成配对")
        wait_until(
            lambda: side_a.mgr.pairing_status() == "paired"
            and side_b.mgr.pairing_status() == "paired",
            timeout=10,
            desc="双端已配对",
        )
        log.info("✅ 配对成功")

        # 2. 发送 → 播报 → 确认 → delivered
        log.info("=== 发送 → 播报 → 确认 → delivered ===")
        assert side_a.svc.propose("告诉TA今晚早点睡") is True, "触发词应命中"
        rid1 = side_a.confirms[-1].id
        side_a.svc.confirm_send(rid1)
        wait_until(lambda: rid1 in side_b.notify.shown, 10, "B 收到并播报")
        assert side_b.notify.shown_count == 1, "首次播报仅一次"
        side_b.notify.click_ack(rid1)  # 模拟点击"知道了"
        wait_until(
            lambda: side_b.store.get(rid1).status == CarryStatus.DELIVERED,
            10, "B 端 delivered",
        )
        wait_until(
            lambda: side_a.store.get(rid1).status == CarryStatus.DELIVERED,
            10, "A 端 delivered",
        )
        assert side_a.store.get(rid1).direction == "out"
        assert side_b.store.get(rid1).direction == "in"
        log.info("✅ 正常送达（A out=delivered，B in=delivered）")

        # 3. 撤回分支（B 未确认 → 即时隐藏）
        log.info("=== 撤回（B 未确认，即时隐藏）===")
        assert side_a.svc.propose("告诉TA我加班") is True
        rid2 = side_a.confirms[-1].id
        side_a.svc.confirm_send(rid2)
        wait_until(lambda: rid2 in side_b.notify.shown, 10, "B 收到第二条")
        side_a.svc.revoke(rid2)  # 窗口内撤回（B 未点确认）
        wait_until(
            lambda: side_a.store.get(rid2).status == CarryStatus.REVOKED,
            10, "A 端 revoked",
        )
        wait_until(
            lambda: side_b.store.get(rid2).status == CarryStatus.REVOKED,
            10, "B 端 revoked",
        )
        assert rid2 in side_b.notify.hides, "B 端 hide_carry 应被调"
        log.info("✅ 撤回成功（A/B 均 revoked，B 即时隐藏）")

        # 4. 过期分支
        log.info("=== 过期 → failed ===")
        now = int(time.time())
        rec3 = side_a.store.create_outgoing("过期消息", expires_at=now - 1)
        assert side_a.store.mark_sent(rec3.id, now - 5)
        side_a.svc.scan_expired(now)
        assert side_a.store.get(rec3.id).status == CarryStatus.FAILED
        log.info("✅ 过期失败标记成功")

        # 5. 全程断言：无重复播报、状态收敛
        assert side_b.notify.shown_count == 2, "全程无重复播报"
        assert any(
            r.id == rid1 and r.status == CarryStatus.DELIVERED
            for r in side_a.statuses
        ), "A 端 on_status 应含 delivered 通知"
        log.info("=== 全部通过 ===")
    finally:
        for s in (side_a, side_b):
            try:
                s.mgr.stop()
            except Exception:  # noqa: BLE001
                pass
            s.db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="带话双端联调脚本")
    parser.add_argument("--debug", action="store_true", help="输出 debug 日志")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if not args.debug:
        logging.getLogger("zeroconf").setLevel(logging.WARNING)
        logging.getLogger("websockets").setLevel(logging.WARNING)

    try:
        run_demo()
    except Exception as exc:  # noqa: BLE001
        log.error("联调失败: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
