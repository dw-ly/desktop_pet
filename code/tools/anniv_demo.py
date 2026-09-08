"""纪念日双端联调脚本（对应 anniversary impl §8.2 / plan C2）。

复用 consistency/pet_growth 双端骨架（prewrite_identity + derive_mac_key + 配对），
演示并断言纪念日全链路：

    双端配对
    A 添加"在一起纪念日"（solar/yearly/今天）→ date.add 同步 → B 列表一致
    提前 1 天 → 双端 remind 预告（on_remind 收到祝福文案）
    当天 → 双端 celebrate + date.remind 双向 → 限时装扮解锁（本端 + 接收端）
    限时装扮过期 → 回归原始装扮
    钩子加成：register_anniversary_hook → A award('feed')==6（纪念日 ×2）→ B 合入
    农历"七夕" → lunar_to_solar 换算当年公历 → 触发 celebrate
    改名 update → LWW 覆盖 → B 一致；删除 → 墓碑 → B 移除
    收尾：双端 anniversaries LWW 收敛一致；date.add/date.remind payload 键纯净

用法（在 code 目录下）：
    .venv\\Scripts\\python tools\\anniv_demo.py            # 常规（INFO）
    .venv\\Scripts\\python tools\\anniv_demo.py --debug   # 含帧级日志
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core import init_core  # noqa: E402
from core.anniv_calendar import AnnivCalendar, lunar_to_solar  # noqa: E402
from core.anniv_celebrate import AnnivCelebrate  # noqa: E402
from core.anniv_integration import (  # noqa: E402
    register_anniversary_hook,
    unregister_anniversary_hook,
)
from core.anniv_store import AnnivStore  # noqa: E402
from core.anniv_sync import AnnivSync  # noqa: E402
from core.pet_growth import PetGrowth  # noqa: E402
from core.pet_sync import PetSync  # noqa: E402
from sync import EventType, SyncConfig, SyncManager  # noqa: E402
from sync.crypto import derive_mac_key, format_code, generate_identity  # noqa: E402
from sync.key_store import KeyStore  # noqa: E402
from sync.manager import PairingEvent  # noqa: E402

log = logging.getLogger("anniv_demo")

OUTFIT_ITEM = "anniv_limited_suit"


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


def _entry_key(e: dict) -> tuple:
    return (e["id"], e["title"], e["date"], e["calendar"], e["repeat"],
            e["notify_days_before"], e["updated_at"])


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
    store = AnnivStore(db)
    cal = AnnivCalendar(db)
    remind_msgs: list[tuple[dict, str]] = []
    celebrate_count = {"total": 0}
    received_remind = {"count": 0}
    pet_sync = PetSync(db, mgr, mac_key)
    celebrate = AnnivCelebrate(
        db, mgr, cal,
        on_remind=lambda e, text: remind_msgs.append((e, text)),
        on_celebrate=lambda e: celebrate_count.__setitem__(
            "total", celebrate_count["total"] + 1),
    )
    svc = AnnivSync(db, mgr, store)

    def on_date_remind(m) -> bool:
        ok = celebrate.handle_date_remind(m)
        if ok:
            received_remind["count"] += 1
            log.info("[%s] 收到 date.remind id=%s → 一同庆祝", label, m.payload.get("id"))
        return ok

    mgr.add_handler(EventType.DATE_ADD, svc.handle_date_add)
    mgr.add_handler(EventType.DATE_REMIND, on_date_remind)
    mgr.add_handler(EventType.PET_FEED, pet_sync.handle_pet_feed)
    return SimpleNamespace(
        mgr=mgr, db=db, store=store, cal=cal, celebrate=celebrate, svc=svc,
        pet_sync=pet_sync, remind=remind_msgs, celebrate_count=celebrate_count,
        received_remind=received_remind,
    )


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def run_demo(port_range=(48000, 48099)) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tuanzi-anniv-demo-"))
    dir_a, dir_b = str(tmp / "a"), str(tmp / "b")

    a_kp, b_kp = generate_identity(), generate_identity()
    prewrite_identity(dir_a, a_kp.private_key)
    prewrite_identity(dir_b, b_kp.private_key)
    mac_key = derive_mac_key(a_kp.private_key, b_kp.public_key)

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

    side_a = make_side(dir_a, "annivA", mac_key, port_range, on_pairing_a)
    side_b = make_side(dir_b, "annivB", mac_key, port_range, on_pairing_b)
    growth_a = PetGrowth(side_a.db, side_a.mgr, mac_key)

    try:
        today = date.today()
        day_before = today - timedelta(days=1)

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
        log.info("✅ 配对成功")

        # 1. A 添加纪念日 → date.add 同步 → B 列表一致
        log.info("=== A 添加纪念日 → 同步 ===")
        solar = side_a.svc.add({
            "title": "在一起纪念日",
            "date": today.strftime("%m-%d"),
            "calendar": "solar",
            "repeat": "yearly",
            "notify_days_before": 1,
        })
        solar_id = solar["id"]
        wait_until(lambda: len(side_b.store.list_all()) == 1, 10, "B 收到 date.add")
        assert [_entry_key(e) for e in side_a.store.list_all()] == [
            _entry_key(e) for e in side_b.store.list_all()
        ]
        log.info("✅ B 列表与 A 一致（%s，%s）", solar["title"], solar["date"])

        # 2. 提前 1 天 → 双端 remind 预告
        log.info("=== 提前 1 天 → 预告 ===")
        acts_a = side_a.celebrate.daily_check(day_before)
        acts_b = side_b.celebrate.daily_check(day_before)
        assert f"remind:{solar_id}" in acts_a
        assert f"remind:{solar_id}" in acts_b
        assert side_a.remind and side_b.remind
        assert side_a.remind[0][1]  # 非空祝福文案
        assert side_a.remind[0][0]["id"] == solar_id
        log.info("✅ 双端预告：%s", side_a.remind[0][1])

        # 3. 当天 → 双端 celebrate + date.remind 双向
        log.info("=== 当天 → 庆祝 + date.remind 双向 ===")
        acts_a = side_a.celebrate.daily_check(today)
        acts_b = side_b.celebrate.daily_check(today)
        assert f"celebrate:{solar_id}" in acts_a
        assert f"celebrate:{solar_id}" in acts_b
        assert side_a.celebrate.current_outfit() == OUTFIT_ITEM
        assert side_b.celebrate.current_outfit() == OUTFIT_ITEM
        wait_until(lambda: side_a.received_remind["count"] >= 1, 10, "A 收到对方 date.remind")
        wait_until(lambda: side_b.received_remind["count"] >= 1, 10, "B 收到对方 date.remind")
        for side in (side_a, side_b):
            rows = side.db.query_all(
                "SELECT payload_json FROM events WHERE type='date.remind' AND status='sent'"
            )
            assert len(rows) == 1, f"{side} 应发送 1 条 date.remind"
        log.info("✅ 双端限时装扮解锁 + date.remind 互相送达（收到计数 A=%s B=%s）",
                 side_a.received_remind["count"], side_b.received_remind["count"])

        # 4. 限时装扮过期 → 回归原始装扮
        log.info("=== 限时装扮过期回归 ===")
        assert side_a.celebrate.check_outfit_expiry(today + timedelta(days=1)) == OUTFIT_ITEM
        assert side_a.celebrate.current_outfit() is None
        log.info("✅ 过期回归原始装扮")

        # 5. 钩子加成：纪念日当天 ×2
        log.info("=== 钩子加成（纪念日 ×2）===")
        register_anniversary_hook(side_a.cal)
        from core.pet_growth import is_anniversary_today  # noqa: E402
        assert is_anniversary_today() is True
        assert growth_a.award("feed") == 6  # 3 × 2
        wait_until(lambda: _intimacy(side_b.db) >= 6, 10, "B 合入 A 的 pet.feed")
        assert _intimacy(side_a.db) == _intimacy(side_b.db) == 6
        log.info("✅ A 喂食 +6（×2），双端亲密度 6")

        # 6. 农历纪念日（七夕）→ 换算当年公历 → 触发
        log.info("=== 农历七夕换算触发 ===")
        qixi_occ = lunar_to_solar(today.year, 7, 7)
        qixi = side_a.svc.add({
            "title": "七夕",
            "date": "07-07",
            "calendar": "lunar",
            "repeat": "yearly",
            "notify_days_before": 1,
        })
        qixi_id = qixi["id"]
        wait_until(lambda: len(side_b.store.list_all()) == 2, 10, "B 收到七夕")
        acts = side_a.celebrate.daily_check(qixi_occ)
        assert f"celebrate:{qixi_id}" in acts
        assert side_a.celebrate.current_outfit() == OUTFIT_ITEM
        log.info("✅ 七夕（农历 07-07）→ 公历 %s 触发庆祝", qixi_occ.isoformat())

        # 7. 改名 update → LWW 覆盖 → B 一致
        log.info("=== 改名同步（LWW）===")
        side_a.svc.update(solar_id, {"title": "在一起 100 天"}, now=int(time.time()) + 100)
        wait_until(
            lambda: any(e["id"] == solar_id and e["title"] == "在一起 100 天"
                        for e in side_b.store.list_all()),
            10, "B 收到改名",
        )
        log.info("✅ B 标题已更新为「在一起 100 天」")

        # 8. 删除 → 墓碑 → B 移除
        log.info("=== 删除同步（墓碑）===")
        # 墓碑 updated_at 必须 > 改名时的 T+100，否则 B 端 LWW 会拒绝旧墓碑
        assert side_a.svc.delete(solar_id, now=int(time.time()) + 200) is True
        wait_until(
            lambda: all(e["id"] != solar_id for e in side_b.store.list_all()),
            10, "B 收到删除",
        )
        assert [e["id"] for e in side_a.store.list_all()] == [qixi_id]
        log.info("✅ 删除同步，B 已移除该纪念日")

        # 9. 收尾：LWW 收敛 + payload 纯净
        log.info("=== 收尾断言 ===")
        assert [_entry_key(e) for e in side_a.store.list_all()] == [
            _entry_key(e) for e in side_b.store.list_all()
        ]
        for side in (side_a, side_b):
            rows = side.db.query_all(
                "SELECT type, payload_json FROM events "
                "WHERE type IN ('date.add', 'date.remind')"
            )
            for r in rows:
                payload = json.loads(r["payload_json"])
                if r["type"] == "date.remind":
                    assert set(payload.keys()) == {"id", "title", "date", "calendar"}
                else:  # date.add
                    assert payload["deleted"] in (True, False)
                    if payload["deleted"]:
                        assert set(payload.keys()) == {"id", "deleted", "updated_at"}
                    else:
                        assert set(payload.keys()) == {
                            "id", "title", "date", "calendar", "repeat",
                            "notify_days_before", "updated_at", "deleted",
                        }
        log.info("✅ date.add/date.remind payload 键纯净；双端纪念日列表 LWW 收敛一致")

        log.info("=== 全部通过 ===")
    finally:
        unregister_anniversary_hook()
        for s in (side_a, side_b):
            try:
                s.mgr.stop()
            except Exception:  # noqa: BLE001
                pass
            s.db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _intimacy(db) -> int:
    row = db.query_one("SELECT value FROM pet_state WHERE key='intimacy'")
    return int(row["value"]) if row else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="纪念日双端联调脚本")
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
