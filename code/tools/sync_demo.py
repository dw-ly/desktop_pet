"""同步层双端联调脚本（对应 impl §12 / plan C2）。

一键验证：配对 → 互发 → 离线 → 重连补发 → 吊销。

用法（在 code 目录下）：
    .venv\\Scripts\\python tools\\sync_demo.py
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core import init_core  # noqa: E402
from sync import EventType, SyncConfig, SyncManager  # noqa: E402
from sync.events import Message  # noqa: E402
from sync.manager import PairingEvent  # noqa: E402
from sync.crypto import format_code  # noqa: E402

log = logging.getLogger("sync_demo")


def make_manager(
    data_dir: str,
    label: str,
    inbox: list,
    port_range: tuple,
    on_pairing=None,
) -> tuple[SyncManager, object]:
    cfg = SyncConfig(data_dir=data_dir, instance_id=label, ws_port_range=port_range)
    db = init_core(data_dir)  # 统一 core.db（收编后队列/去重落在 events 表）
    mgr = SyncManager(
        cfg,
        db=db,
        on_event=lambda m: inbox.append(m),
        on_state=lambda s: log.info("[%s] 连接状态=%s", label, s.value),
        on_pairing=on_pairing or (lambda e, d: log.info("[%s] 配对事件=%s %s", label, e.value, d)),
    )
    return mgr, db


def wait_until(pred, timeout: float, desc: str) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.1)
    raise RuntimeError(f"等待超时: {desc}")


def run_demo(port_range=(48000, 48099)) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tuanzi-demo-"))
    dir_a = str(tmp / "a")
    dir_b = str(tmp / "b")

    inbox_a: list[Message] = []
    inbox_b: list[Message] = []

    auto_confirm = threading.Event()

    def on_pairing_a(e: PairingEvent, d: dict) -> None:
        log.info("[A] 配对事件=%s %s", e.value, d)
        if e == PairingEvent.PEER_REQUEST:
            peer_id = d["peer_id"]
            # 模拟 UI 确认：后台线程调用，避免阻塞 A 的 loop
            threading.Thread(target=lambda: mgr_a.confirm_peer(peer_id), daemon=True).start()
        if e == PairingEvent.PAIRED:
            auto_confirm.set()

    def on_pairing_b(e: PairingEvent, d: dict) -> None:
        log.info("[B] 配对事件=%s %s", e.value, d)

    mgr_a, db_a = make_manager(dir_a, "demoA", inbox_a, port_range, on_pairing=on_pairing_a)
    mgr_b, db_b = make_manager(dir_b, "demoB", inbox_b, port_range, on_pairing=on_pairing_b)

    try:
        # 1. 启动
        log.info("=== 启动双端 ===")
        mgr_a.start()
        mgr_b.start()

        # 2. 配对
        log.info("=== 配对 ===")
        code = mgr_a.start_pairing()
        log.info("[A] 配对码: %s", format_code(code))
        mgr_b.confirm_pairing(code)
        wait_until(lambda: auto_confirm.is_set(), timeout=15, desc="A 完成配对")
        wait_until(
            lambda: mgr_a.pairing_status() == "paired" and mgr_b.pairing_status() == "paired",
            timeout=10,
            desc="双端已配对",
        )
        log.info("✅ 配对成功")

        # 3. 互发（在线直发）
        log.info("=== 在线互发 ===")
        mgr_a.send(EventType.MSG_CARRY, {"text": "在吗", "carryId": "c1", "expireAt": int(time.time()) + 3600})
        mgr_b.send(EventType.MSG_CARRY, {"text": "在的", "carryId": "c2", "expireAt": int(time.time()) + 3600})
        wait_until(lambda: any(m.type == "msg.carry" and m.payload.get("carryId") == "c1" for m in inbox_b), timeout=10, desc="B 收到 A 消息")
        wait_until(lambda: any(m.type == "msg.carry" and m.payload.get("carryId") == "c2" for m in inbox_a), timeout=10, desc="A 收到 B 消息")
        log.info("✅ 在线互发成功")

        # 4. 离线入队 + 重连补发
        log.info("=== 离线补发 ===")
        mgr_b.stop()
        db_b.close()
        # 必须等 A 处理完 B 的断开（脱离 CONNECTED）再发：
        # 否则 A 仍认为在线，消息直发进半死 socket（ws.send 不抛异常）而静默丢失。
        # 注意主动方重连循环会把状态置 CONNECTING/RECONNECTING，故只判"不等于 CONNECTED"。
        wait_until(
            lambda: mgr_a.connection_status().value != "connected",
            timeout=10,
            desc="A 检测到 B 离线",
        )
        for i in range(3):
            mgr_a.send(EventType.MSG_CARRY, {"text": f"离线消息{i}", "carryId": f"off{i}", "expireAt": int(time.time()) + 3600})
        log.info("[A] 已入队 3 条离线消息")

        mgr_b, db_b = make_manager(dir_b, "demoB", inbox_b, port_range, on_pairing=on_pairing_b)
        mgr_b.start()
        wait_until(
            lambda: len([m for m in inbox_b if m.type == "msg.carry" and m.payload.get("carryId", "").startswith("off")]) >= 3,
            timeout=20,
            desc="B 补齐离线消息",
        )
        seqs = [m.seq for m in inbox_b if m.type == "msg.carry" and m.payload.get("carryId", "").startswith("off")]
        assert seqs == sorted(seqs), f"补发未按 seq 升序: {seqs}"
        assert len(seqs) == len(set(seqs)), "补发存在重复"
        log.info("✅ 离线补发成功（按 seq 升序、无重复）")

        # 5. 吊销
        log.info("=== 吊销配对 ===")
        mgr_a.revoke_pairing()
        wait_until(lambda: mgr_a.pairing_status() == "unpaired", timeout=8, desc="A 端未配对")
        wait_until(lambda: mgr_b.pairing_status() == "unpaired", timeout=15, desc="B 端收到吊销")
        log.info("✅ 吊销成功")

        log.info("=== 全部通过 ===")
    finally:
        for m in (mgr_a, mgr_b):
            try:
                m.stop()
            except Exception:
                pass
        for d in (db_a, db_b):
            try:
                d.close()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="同步层双端联调脚本")
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
