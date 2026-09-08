"""共同养成双端联调脚本（对应 pet-growth impl §8.2 / plan C2）。

复用 consistency_demo 双端骨架（prewrite_identity + derive_mac_key + 配对），
演示并断言共同养成全链路：

    A 喂食 ×5（+3×5）→ 双端亲密度 15；第 6 次超上限拒绝（0）
    A 带话（+10）→ 双端 25；A 再带话（<5min）→ 频率门控拒绝（0）
    B 共同对话（+5）→ 双端 30
    伪造签名 pet.feed → B 拒绝（亲密度不变）
    批量大额事件（+10000, reason=gift）→ 双端 10030 → 等级 10 → 双端解锁 5 项
    每日结算：双活跃 → 生成方发 streak 事件，双端 streak=1、亲密度 +2
    灰色期：次日仅 A 活跃 → 双端转 grace；连续 3 日双活跃 → 恢复 normal
    改名：A set_my_pet_name("小团子") → B peer_pet_name 同步
    形象：A list_presets() 3 款；A set_preset("moon-cat") → 仅本端生效

全程断言：payload 键 == {delta, reason, ts, sig}、上限/门控/验签/状态机/改名正确、
双端亲密度收敛一致。

用法（在 code 目录下）：
    .venv\\Scripts\\python tools\\pet_growth_demo.py            # 常规（INFO）
    .venv\\Scripts\\python tools\\pet_growth_demo.py --debug   # 含帧级日志
"""

from __future__ import annotations

import argparse
import base64
import itertools
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core import apply_intimacy_event, init_core, sign_intimacy_event  # noqa: E402
from core.pet_config import load_pet_config  # noqa: E402
from core.pet_growth import PetGrowth  # noqa: E402
from core.pet_level import PetLevel  # noqa: E402
from core.pet_profile import PetProfile  # noqa: E402
from core.pet_streak import PetStreak, SettleStatus  # noqa: E402
from core.pet_sync import PetSync  # noqa: E402
from sync import EventType, SyncConfig, SyncManager  # noqa: E402
from sync.crypto import derive_mac_key, format_code, generate_identity  # noqa: E402
from sync.events import Message  # noqa: E402
from sync.key_store import KeyStore  # noqa: E402
from sync.manager import PairingEvent  # noqa: E402

log = logging.getLogger("pet_growth_demo")

UNLOCKS_EXPECTED = {
    "unlock_level_5",
    "unlock_level_10",
    "unlock_intimacy_100",
    "unlock_intimacy_365",
    "unlock_intimacy_1000",
}

_counter = itertools.count()


def wait_until(pred, timeout: float, desc: str) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.1)
    raise RuntimeError(f"等待超时: {desc}")


def prewrite_identity(data_dir: str, priv: bytes) -> None:
    """身份私钥写入 data_dir/keys/identity.key（绕过 keyring，保证确定性）。"""
    ks = KeyStore(data_dir)
    ks.delete_identity()
    d = Path(data_dir) / "keys"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "identity.key"
    p.write_text(base64.b64encode(priv).decode("ascii"), encoding="ascii")
    os.chmod(p, 0o600)


def _intimacy(db) -> int:
    row = db.query_one("SELECT value FROM pet_state WHERE key='intimacy'")
    return int(row["value"]) if row else 0


def _streak(db) -> int:
    row = db.query_one("SELECT value FROM pet_state WHERE key='streak_days'")
    return int(row["value"]) if row else 0


def _seed(db, my_id: str, day_start: int, *, local: bool, peer: bool) -> None:
    """向 events 表播种某日活动（created_at 落在 day 窗口内）供 settle 判定。"""
    n = next(_counter)
    ts = day_start + 3600  # 当日 01:00
    if local:
        db.execute(
            "INSERT INTO events(event_id, seq, type, peer, payload_json, created_at, status) "
            "VALUES(?, ?, 'pet.feed', 'peer', '{}', ?, 'sent')",
            (f"{my_id}:{n}0", n * 10, ts),
        )
    if peer:
        db.execute(
            "INSERT INTO events(event_id, seq, type, peer, payload_json, created_at, status) "
            "VALUES(?, ?, 'pet.feed', 'peer', '{}', ?, 'received')",
            (f"peer:{n}1", n * 10 + 1, ts),
        )


def _day_noon(d: datetime) -> int:
    return int(datetime(d.year, d.month, d.day, 12).timestamp())


def _day_start(d: datetime) -> int:
    return int(time.mktime((d.year, d.month, d.day, 0, 0, 0, 0, 0, -1)))


def bulk_feed(mgr, db, mac_key: bytes, delta: int, reason: str = "gift") -> bool:
    """模拟一次性大额奖励：签名发送 + 本端合入（绕过每日上限演示批量经验）。"""
    ts = int(time.time())
    payload = {
        "delta": delta,
        "reason": reason,
        "ts": ts,
        "sig": sign_intimacy_event(mac_key, delta, reason, ts),
    }
    mgr.send(EventType.PET_FEED, payload)
    return apply_intimacy_event(db, payload, mac_key)


# --------------------------------------------------------------------------- #
# 一端装配
# --------------------------------------------------------------------------- #

def make_side(data_dir: str, label: str, mac_key: bytes, port_range: tuple,
              on_pairing) -> SimpleNamespace:
    cfg = SyncConfig(data_dir=data_dir, instance_id=label, ws_port_range=port_range)
    db = init_core(data_dir)
    mgr = SyncManager(
        cfg,
        db=db,
        on_state=lambda s: log.info("[%s] 连接状态=%s", label, s.value),
        on_pairing=on_pairing,
    )
    received_feed: list[Message] = []
    pet_sync = PetSync(db, mgr, mac_key)
    pet_profile = PetProfile(db, mgr)

    def on_pet_feed(m: Message) -> None:
        received_feed.append(m)
        ok = pet_sync.handle_pet_feed(m)
        log.info("[%s] 收到 pet.feed delta=%s reason=%s → %s", label,
                 m.payload.get("delta"), m.payload.get("reason"),
                 "合入" if ok else "拒绝")

    def on_pet_profile(m: Message) -> None:
        ok = pet_profile.handle_pet_profile(m)
        log.info("[%s] 收到 pet.profile name=%s → %s", label,
                 m.payload.get("name"), "已存" if ok else "拒绝")

    mgr.add_handler(EventType.PET_FEED, on_pet_feed)
    mgr.add_handler(EventType.PET_PROFILE, on_pet_profile)
    return SimpleNamespace(mgr=mgr, db=db, pet_sync=pet_sync, pet_profile=pet_profile,
                           received_feed=received_feed)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def run_demo(port_range=(48000, 48099)) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tuanzi-pet-growth-demo-"))
    dir_a, dir_b = str(tmp / "a"), str(tmp / "b")

    a_kp, b_kp = generate_identity(), generate_identity()
    prewrite_identity(dir_a, a_kp.private_key)
    prewrite_identity(dir_b, b_kp.private_key)
    mac_key = derive_mac_key(a_kp.private_key, b_kp.public_key)
    cfg = load_pet_config()

    auto_confirm = threading.Event()
    side_a = side_b = None

    def on_pairing_a(e: PairingEvent, d: dict) -> None:
        log.info("[A] 配对事件=%s %s", e.value, d)
        if e == PairingEvent.PEER_REQUEST:
            threading.Thread(
                target=lambda: side_a.mgr.confirm_peer(d["peer_id"]), daemon=True
            ).start()
        if e == PairingEvent.PAIRED:
            auto_confirm.set()

    def on_pairing_b(e: PairingEvent, d: dict) -> None:
        log.info("[B] 配对事件=%s %s", e.value, d)

    side_a = make_side(dir_a, "petA", mac_key, port_range, on_pairing_a)
    side_b = make_side(dir_b, "petB", mac_key, port_range, on_pairing_b)

    growth_a = PetGrowth(side_a.db, side_a.mgr, mac_key, cfg)
    growth_b = PetGrowth(side_b.db, side_b.mgr, mac_key, cfg)
    level_a = PetLevel(side_a.db, cfg)
    level_b = PetLevel(side_b.db, cfg)
    streak_a = PetStreak(side_a.db, growth_a, cfg)
    streak_b = PetStreak(side_b.db, growth_b, cfg)

    try:
        log.info("=== 启动双端 + 配对 ===")
        side_a.mgr.start()
        side_b.mgr.start()
        code = side_a.mgr.start_pairing()
        log.info("[A] 配对码: %s", format_code(code))
        side_b.mgr.confirm_pairing(code)
        wait_until(lambda: auto_confirm.is_set(), 15, "A 完成配对")
        wait_until(
            lambda: side_a.mgr.pairing_status() == "paired"
            and side_b.mgr.pairing_status() == "paired",
            timeout=10, desc="双端已配对",
        )
        a_peer = side_a.mgr.peer_id()
        b_peer = side_b.mgr.peer_id()
        log.info("✅ 配对成功（A=%s B=%s）", a_peer, b_peer)

        # 1. A 喂食 ×5 → 双端 15；第 6 次超上限拒绝
        log.info("=== A 喂食 ×5 + 超上限拒绝 ===")
        for i in range(5):
            assert growth_a.award("feed") == 3, f"第 {i + 1} 次喂食应合入"
        assert growth_a.award("feed") == 0  # 当日第 6 次 → 拒绝
        wait_until(lambda: _intimacy(side_b.db) >= 15, 10, "B 合入 A 的 5 条 feed")
        assert _intimacy(side_a.db) == _intimacy(side_b.db) == 15
        log.info("✅ 双端亲密度 15；第 6 次喂食被拒（上限）")

        # 2. A 带话 +10；5min 内重复 → 门控拒绝
        log.info("=== A 带话（5min 频率门控）===")
        assert growth_a.award("carry") == 10
        assert growth_a.award("carry") == 0  # <5min → 门控拒绝
        wait_until(lambda: _intimacy(side_b.db) >= 25, 10, "B 合入 carry")
        assert _intimacy(side_a.db) == _intimacy(side_b.db) == 25
        log.info("✅ 双端 25；A 重复带话被门控拒绝")

        # 3. B 共同对话 +5 → 双端 30（B 产生本端活动，支撑今日双活跃判定）
        log.info("=== B 共同对话 +5 ===")
        assert growth_b.award("chat") == 5
        wait_until(lambda: _intimacy(side_a.db) >= 30, 10, "A 合入 B 的 chat")
        assert _intimacy(side_a.db) == _intimacy(side_b.db) == 30
        log.info("✅ 双端 30")

        # 4. 伪造签名事件 → B 拒绝
        log.info("=== 伪造签名事件被拒 ===")
        ts = int(time.time())
        forged_payload = {
            "delta": 3, "reason": "feed", "ts": ts,
            "sig": sign_intimacy_event(mac_key, 3, "feed", ts),
        }
        forged_payload["delta"] = 999  # 篡改 delta
        forged = Message(v=1, type="pet.feed", from_id=a_peer, seq=999999,
                         ts=ts, payload=forged_payload)
        assert side_b.pet_sync.handle_pet_feed(forged) is False
        assert _intimacy(side_b.db) == 30
        log.info("✅ 伪造事件被拒（验签失败），亲密度不变")

        # 5. 批量大额事件 → 双端 10030 → 等级 10 → 解锁 5 项
        log.info("=== 批量经验 → 升级解锁 ===")
        assert bulk_feed(side_a.mgr, side_a.db, mac_key, 10000) is True
        wait_until(lambda: _intimacy(side_b.db) >= 10030, 10, "B 合入批量事件")
        assert _intimacy(side_a.db) == _intimacy(side_b.db) == 10030
        unlocks_a = level_a.sync()
        unlocks_b = level_b.sync()
        assert set(unlocks_a) == UNLOCKS_EXPECTED
        assert set(unlocks_b) == UNLOCKS_EXPECTED
        assert level_a.current_level() == level_b.current_level() == 10
        log.info("✅ 双端亲密度 10030、等级 10，解锁 5 项：%s", sorted(unlocks_a))

        # 6. 每日结算（今日双活跃）→ 生成方发 streak 事件，双端 streak=1
        log.info("=== 每日结算（今日双活跃）===")
        now_ts = int(time.time())
        out_a = streak_a.settle(a_peer, b_peer, now=now_ts)
        out_b = streak_b.settle(b_peer, a_peer, now=now_ts)
        assert out_a.status == SettleStatus.OK and out_b.status == SettleStatus.OK
        assert out_a.both_active and out_b.both_active
        assert _streak(side_a.db) == 1 and _streak(side_b.db) == 1
        assert (out_a.event_sent or out_b.event_sent)  # 恰一端为生成方
        assert not (out_a.event_sent and out_b.event_sent)
        wait_until(
            lambda: _intimacy(side_a.db) >= 10032 and _intimacy(side_b.db) >= 10032,
            10, "双端合入 streak 积分",
        )
        assert _intimacy(side_a.db) == _intimacy(side_b.db) == 10032
        log.info("✅ 双端 streak=1（生成方=%s），亲密度 10032",
                 "A" if out_a.event_sent else "B")

        # 7. 灰色期：次日仅 A 活跃 → 双端转 grace
        log.info("=== 灰色期（次日仅 A 活跃）===")
        t1 = datetime.now() + timedelta(days=1)
        s1 = _day_start(t1)
        _seed(side_a.db, a_peer, s1, local=True, peer=False)   # A 侧：仅本端活动
        _seed(side_b.db, b_peer, s1, local=False, peer=True)   # B 侧：仅收到 A 活动
        g_a = streak_a.settle(a_peer, b_peer, now=_day_noon(t1))
        g_b = streak_b.settle(b_peer, a_peer, now=_day_noon(t1))
        assert g_a.grace_status == "grace" and g_b.grace_status == "grace"
        assert g_a.grace_left == 3 and g_b.grace_left == 3
        assert _streak(side_a.db) == 1  # 灰色期不清零
        log.info("✅ 双端转灰色期（grace_left=3，streak 保留 1）")

        # 8. 连续 3 日双活跃 → 恢复 normal
        log.info("=== 连续 3 日双活跃 → 恢复 normal ===")
        for i in range(1, 4):
            d = datetime.now() + timedelta(days=1 + i)
            s = _day_start(d)
            _seed(side_a.db, a_peer, s, local=True, peer=True)
            _seed(side_b.db, b_peer, s, local=True, peer=True)
            r_a = streak_a.settle(a_peer, b_peer, now=_day_noon(d))
            r_b = streak_b.settle(b_peer, a_peer, now=_day_noon(d))
            assert r_a.grace_status == r_b.grace_status
        r_a = streak_a.settle(a_peer, b_peer, now=_day_noon(
            datetime.now() + timedelta(days=4)))
        assert r_a.grace_status == "normal"
        assert _streak(side_a.db) == 4  # 1 + 3 日连续恢复
        log.info("✅ 连续 3 日恢复 → normal（streak=4）")

        # 9. 改名同步
        log.info("=== 改名同步 ===")
        side_a.pet_profile.set_my_pet_name("小团子")
        wait_until(
            lambda: side_b.pet_profile.peer_pet_name() == "小团子",
            10, "B 收到宠物名",
        )
        assert side_a.pet_profile.my_pet_name() == "小团子"
        log.info("✅ B 状态栏：TA 的团子：小团子")

        # 10. 形象预设（仅本端生效）
        log.info("=== 形象预设 ===")
        presets = side_a.pet_profile.list_presets()
        assert [p["id"] for p in presets] == ["line-dog-1", "line-dog-2", "moon-cat"]
        side_a.pet_profile.set_preset("moon-cat")
        assert side_a.pet_profile.current_preset() == "moon-cat"
        assert side_b.pet_profile.current_preset() is None  # 不同步
        log.info("✅ 3 款预设可加载；A 选月薪猫，B 不受影响")

        # 11. 全程 payload 纯净断言 + 双端收敛
        log.info("=== 收尾断言 ===")
        wait_until(
            lambda: _intimacy(side_a.db) == _intimacy(side_b.db),
            10, "双端最终收敛",
        )
        assert _intimacy(side_a.db) == _intimacy(side_b.db)
        for db_ in (side_a.db, side_b.db):
            rows = db_.query_all(
                "SELECT payload_json FROM events WHERE type='pet.feed'"
            )
            for r in rows:
                payload = json.loads(r["payload_json"])
                if not payload:  # _seed 播种的结算活动占位事件（无真实载荷）
                    continue
                assert set(payload.keys()) == {"delta", "reason", "ts", "sig"}
        log.info("✅ pet.feed payload 恒为 {delta, reason, ts, sig}（占位事件除外）；双端收敛一致")

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
    parser = argparse.ArgumentParser(description="共同养成双端联调脚本")
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
