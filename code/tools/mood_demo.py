"""情绪同步双端联调脚本（对应 mood-sync impl §7.2 / plan C2）。

复用 consistency/carry_demo 的双端骨架（临时 data_dir + discovery + 配对），
演示并断言情绪全链路：

    A happy（首次发送）→ B 收到，partner_mood=happy，override="idle_happy"
    A sleepy（显著变化，间隔满足）→ B 收到，override="idle_tired"，状态条软化文案
    A 无显著变化（节流）→ 不发送
    A 开启隐身 → B 收到 {"privacy": true}，partner_stealth=True，标签保留
    A 关闭隐身 → B 保持最近标签；A 再次发送 happy → B partner_stealth 复位

全程断言：payload 仅三字段（无文本）、节流边界（0.3 / 600s）正确、隐身停发生效。

用法（在 code 目录下）：
    .venv\\Scripts\\python tools\\mood_demo.py            # 常规（INFO）
    .venv\\Scripts\\python tools\\mood_demo.py --debug   # 含帧级日志

说明：节流时间源注入（now 参数）使 600s 最小间隔可在秒级演示；实际事件经 sync 层
真实加密传输（Envelope 密文透传），B 端收到的为解密后的业务负载。
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core import init_core  # noqa: E402
from core.mood_config import MoodConfig  # noqa: E402
from core.mood_export import Emotion  # noqa: E402
from core.mood_privacy import MoodPrivacy  # noqa: E402
from core.mood_receive import MoodEvent, MoodReceiver  # noqa: E402
from core.mood_sender import MoodSender  # noqa: E402
from sync import EventType, SyncConfig, SyncManager  # noqa: E402
from sync.crypto import format_code  # noqa: E402
from sync.manager import PairingEvent  # noqa: E402
from ui.partner_state import mood_status_text  # noqa: E402

log = logging.getLogger("mood_demo")

# 时间源（节流判定注入）：演示 600s 最小间隔，配对后第 0s 首次发送
T0 = 1000.0


def wait_until(pred, timeout: float, desc: str) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.1)
    raise RuntimeError(f"等待超时: {desc}")


def make_side(data_dir: str, label: str, port_range: tuple, on_pairing) -> SimpleNamespace:
    """一端完整装配：sync + core.db（情绪模块为内存态，db 仅供 sync 层 events 落表）。"""
    cfg = SyncConfig(data_dir=data_dir, instance_id=label, ws_port_range=port_range)
    db = init_core(data_dir)
    mgr = SyncManager(
        cfg,
        db=db,
        on_state=lambda s: log.info("[%s] 连接状态=%s", label, s.value),
        on_pairing=on_pairing,
    )
    return SimpleNamespace(mgr=mgr, db=db)


def run_demo(port_range=(48000, 48099)) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tuanzi-mood-demo-"))
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

    side_a = make_side(dir_a, "moodA", port_range, on_pairing_a)
    side_b = make_side(dir_b, "moodB", port_range, on_pairing_b)

    # B 接收侧：记录 payload（断言纯净）+ MoodEvent（断言状态）
    b_events: list[MoodEvent] = []
    b_payloads: list[dict] = []
    recv_b = MoodReceiver(on_mood_change=b_events.append)

    def on_mood_msg(m) -> None:
        b_payloads.append(m.payload)
        recv_b.handle(m.payload)

    side_b.mgr.add_handler(EventType.MOOD_SYNC, on_mood_msg)

    # A 发送侧：exporter 语义由 demo 直接驱动 maybe_send（显式 now 演示节流）
    recv_a = MoodReceiver()
    privacy_a = MoodPrivacy(sync=side_a.mgr, receiver=recv_a)
    sender_a = MoodSender(
        sync=side_a.mgr,
        config=MoodConfig(threshold=0.3, min_interval=600.0),  # 契约默认
        is_stealth=lambda: privacy_a.is_stealth,
    )

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
            timeout=10, desc="双端已配对",
        )
        log.info("✅ 配对成功")

        # 2. A happy（首次发送，跳过显著变化判定 D21）→ B 收到
        log.info("=== happy 首次发送 ===")
        assert sender_a.maybe_send(Emotion(0.8, 0.6, "happy"), now=T0) is True
        wait_until(lambda: len(b_events) >= 1, 10, "B 收到 happy")
        assert recv_b.partner_mood().label == "happy"
        assert recv_b.animation_override() == "idle_happy"
        assert mood_status_text(recv_b.partner_mood(), recv_b.partner_stealth()) == "TA 心情不错"
        log.info("✅ B: partner_mood=happy, override=idle_happy, 状态条='TA 心情不错'")

        # 3. A sleepy（|Δ|≥0.3 且间隔 700s ≥ 600）→ B 收到 tired 表现
        log.info("=== sleepy 显著变化（间隔 700s ≥ 600s）===")
        assert sender_a.maybe_send(Emotion(0.2, 0.1, "sleepy"), now=T0 + 700) is True
        wait_until(lambda: len(b_events) >= 2, 10, "B 收到 sleepy")
        assert recv_b.partner_mood().label == "sleepy"
        assert recv_b.animation_override() == "idle_tired"
        assert mood_status_text(recv_b.partner_mood(), recv_b.partner_stealth()) == "TA 今天有点累"
        log.info("✅ B: override=idle_tired, 状态条='TA 今天有点累'")

        # 4. A 无显著变化（|Δvalence|=0.1, |Δarousal|=0.1 < 0.3）→ 不发送
        log.info("=== 无显著变化（节流拦截）===")
        n_before = len(b_events)
        assert sender_a.maybe_send(Emotion(0.3, 0.2, "calm"), now=T0 + 1400) is False
        time.sleep(0.5)  # 给足异步传输窗口，确认确实未收到
        assert len(b_events) == n_before
        log.info("✅ 节流：|Δ|<0.3 不发送")

        # 5. A 开启隐身 → B 收到 privacy 通知，标签保留
        log.info("=== 开启隐身 ===")
        n_before = len(b_events)
        privacy_a.set_stealth(True)
        wait_until(lambda: len(b_events) == n_before + 1, 10, "B 收到 privacy 通知")
        assert recv_b.partner_stealth() is True
        assert recv_b.partner_mood().label == "sleepy"  # 最近标签保留（D19）
        assert recv_b.animation_override() == "idle_tired"  # 表现稳定不闪烁
        assert mood_status_text(recv_b.partner_mood(), recv_b.partner_stealth()) == "TA 开启了隐身"
        # 隐身中一切发送停止
        assert sender_a.maybe_send(Emotion(-0.5, 0.8, "sad"), now=T0 + 2100) is False
        log.info("✅ B: stealth=True, 标签保留, 隐身停发生效")

        # 6. A 关闭隐身 → 不发消息，B 保持最近标签；A 再次 happy → B stealth 复位
        log.info("=== 关闭隐身 → 恢复同步 ===")
        n_before = len(b_events)
        privacy_a.set_stealth(False)
        time.sleep(0.5)
        assert len(b_events) == n_before  # 关闭不发任何消息（D19）
        assert recv_b.partner_stealth() is True  # B 仍保持隐身标记（等下次正常帧覆盖）
        assert recv_b.partner_mood().label == "sleepy"
        assert sender_a.maybe_send(Emotion(0.8, 0.6, "happy"), now=T0 + 2800) is True
        wait_until(lambda: len(b_events) == n_before + 1, 10, "B 收到恢复后的 happy")
        assert recv_b.partner_stealth() is False  # 情绪帧复位隐身标记
        assert recv_b.partner_mood().label == "happy"
        log.info("✅ 恢复同步：B stealth 复位, override=idle_happy")

        # 7. 全程断言：payload 纯净
        log.info("=== payload 纯净断言 ===")
        assert len(b_payloads) == 4  # happy + sleepy + privacy + happy
        assert b_payloads[0] == {"valence": 0.8, "arousal": 0.6, "label": "happy"}
        assert b_payloads[1] == {"valence": 0.2, "arousal": 0.1, "label": "sleepy"}
        assert b_payloads[2] == {"privacy": True}
        assert b_payloads[3] == {"valence": 0.8, "arousal": 0.6, "label": "happy"}
        for payload in b_payloads:
            if "privacy" in payload:
                assert set(payload.keys()) == {"privacy"}  # 隐身通知无情绪键
            else:
                assert set(payload.keys()) == {"valence", "arousal", "label"}  # 无任何文本键
        log.info("✅ payload 仅三数值 + 标签，无任何文本")

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
    parser = argparse.ArgumentParser(description="情绪同步双端联调脚本")
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
