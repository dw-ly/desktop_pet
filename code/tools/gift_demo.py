"""互赠礼物双端联调脚本（对应 gift-exchange impl §8.2 / plan C2）。

复用 anniv_demo 双端骨架（prewrite_identity + derive_mac_key + 配对），
演示并断言：

    双端配对
    A 送 outfit → B 收件箱 → B 打开 → 双方解锁 + 亲密度 +5
    未打开 offer 过期（注入短 TTL）→ 两端 expired → A 可重发
    纪念日钩子 → 再送 → 接受 +20
    自定义彩蛋互送
    user_items expire_at 到期清理

用法（在 code 目录下）：
    ../code/.venv/bin/python tools/gift_demo.py
    ../code/.venv/bin/python tools/gift_demo.py --debug
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core import init_core  # noqa: E402
from core.gift_config import CUSTOM_EGG_ID, GiftState, load_gift_config  # noqa: E402
from core.gift_expire import GiftExpire  # noqa: E402
from core.gift_receive import GiftReceive  # noqa: E402
from core.gift_send import GiftSend  # noqa: E402
from core.gift_store import GiftStore  # noqa: E402
from core.pet_growth import set_anniversary_hook  # noqa: E402
from sync import EventType, SyncConfig, SyncManager  # noqa: E402
from sync.crypto import derive_mac_key, format_code, generate_identity  # noqa: E402
from sync.key_store import KeyStore  # noqa: E402
from sync.manager import PairingEvent  # noqa: E402

log = logging.getLogger("gift_demo")

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "assets" / "manifest.json"
OUTFIT = "outfit-heart-100"


def wait_until(pred, timeout: float, desc: str) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.1)
    raise RuntimeError(f"等待超时: {desc}")


def prewrite_identity(data_dir: str, priv: bytes) -> None:
    ks = KeyStore(data_dir)
    ks.delete_identity()
    d = Path(data_dir) / "keys"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "identity.key"
    p.write_text(base64.b64encode(priv).decode("ascii"), encoding="ascii")
    os.chmod(p, 0o600)


def intimacy(db) -> int:
    row = db.query_one("SELECT value FROM pet_state WHERE key='intimacy'")
    return int(row["value"]) if row else 0


def make_side(
    data_dir: str,
    label: str,
    mac_key: bytes,
    port_range: tuple,
    on_pairing,
    cfg,
) -> SimpleNamespace:
    scfg = SyncConfig(data_dir=data_dir, instance_id=label, ws_port_range=port_range)
    db = init_core(data_dir)
    mgr = SyncManager(
        scfg,
        db=db,
        on_state=lambda s: log.info("[%s] 连接状态=%s", label, s.value),
        on_pairing=on_pairing,
    )
    store = GiftStore(db)
    inbox: list[str] = []
    unlocked: list[str] = []
    expired: list[str] = []

    sender = GiftSend(
        store, mgr, mac_key, cfg=cfg,
        my_peer_id=lambda: mgr.peer_id(),
        partner_id=lambda: mgr.partner_id(),
    )
    recv = GiftReceive(
        store, mgr, mac_key, cfg=cfg,
        on_inbox=lambda o: inbox.append(o.gift_id),
        on_unlocked=lambda gid, iid: unlocked.append(iid),
    )
    exp = GiftExpire(
        store, mgr, cfg=cfg,
        on_offer_expired=lambda o: expired.append(o.gift_id),
    )

    mgr.add_handler(EventType.GIFT_SEND, recv.handle_send)
    mgr.add_handler(EventType.GIFT_ACCEPT, recv.handle_accept)
    mgr.add_handler(EventType.GIFT_EXPIRE, exp.handle_expire)

    return SimpleNamespace(
        mgr=mgr, db=db, store=store, sender=sender, recv=recv, exp=exp,
        inbox=inbox, unlocked=unlocked, expired=expired, label=label,
    )


def run_demo(port_range=(48100, 48199)) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tuanzi-gift-demo-"))
    dir_a, dir_b = str(tmp / "a"), str(tmp / "b")

    # 短 TTL 便于演示过期（2 秒）；正式默认 24h
    cfg = load_gift_config(
        {"offer_ttl_seconds": 2, "egg_max": 20},
        manifest_path=MANIFEST,
    )
    assert len(cfg.catalog) == 17, "礼物库应加载 17 款"

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

    side_a = make_side(dir_a, "giftA", mac_key, port_range, on_pairing_a, cfg)
    side_b = make_side(dir_b, "giftB", mac_key, port_range, on_pairing_b, cfg)

    try:
        set_anniversary_hook(lambda: False)

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
        log.info("PASS 配对成功")

        # 1. A 送 outfit → B 接受 → 双方解锁 + 亲密度 +5
        log.info("=== A 送 %s → B 打开 ===", OUTFIT)
        assert side_a.sender.propose(OUTFIT) is not None
        offer1 = side_a.sender.confirm()
        assert offer1 is not None
        wait_until(lambda: OUTFIT in [
            o.item_id for o in side_b.store.list_offers(state=GiftState.SENT)
        ] or any(o.gift_id == offer1.gift_id for o in side_b.store.list_offers()),
                   10, "B 收到 gift.send")
        assert side_b.recv.accept(offer1.gift_id)
        wait_until(
            lambda: side_a.store.get_offer(offer1.gift_id)
            and side_a.store.get_offer(offer1.gift_id).state == GiftState.ACCEPTED,
            10, "A 收到 gift.accept",
        )
        wait_until(lambda: intimacy(side_a.db) >= 5, 5, "A 亲密度+5")
        wait_until(lambda: intimacy(side_b.db) >= 5, 5, "B 亲密度+5")
        assert side_a.store.get_item(OUTFIT) is not None
        assert side_b.store.get_item(OUTFIT) is not None
        log.info(
            "PASS 双方解锁 + 亲密度 A=%s B=%s",
            intimacy(side_a.db), intimacy(side_b.db),
        )
        base_a, base_b = intimacy(side_a.db), intimacy(side_b.db)

        # 2. 未打开 → 过期退回（短 TTL）→ 可重发
        log.info("=== 未打开过期（TTL=2s）===")
        assert side_a.sender.propose("outfit-paw-scarf")
        offer2 = side_a.sender.confirm()
        assert offer2 is not None
        wait_until(
            lambda: side_b.store.get_offer(offer2.gift_id) is not None,
            10, "B 收到第二份 gift.send",
        )
        # 注入时钟越过 expire_at（严格 <；避免 sleep 卡在整秒边界）
        wait_until(
            lambda: side_b.store.get_offer(offer2.gift_id) is not None,
            10, "B 已入库第二份 offer（过期扫描前）",
        )
        expired = side_a.exp.scan_offers(now=offer2.expire_at + 1)
        assert offer2.gift_id in expired, f"expected {offer2.gift_id} in {expired}"
        wait_until(
            lambda: side_b.store.get_offer(offer2.gift_id)
            and side_b.store.get_offer(offer2.gift_id).state == GiftState.EXPIRED,
            10, "B 收到 gift.expire",
        )
        assert side_a.store.get_offer(offer2.gift_id).state == GiftState.EXPIRED
        assert side_a.store.get_item("outfit-paw-scarf") is None  # pending 回退
        log.info("PASS 过期退回两端一致")

        # 重发
        log.info("=== 重发 outfit-paw-scarf → B 打开 ===")
        assert side_a.sender.propose("outfit-paw-scarf")
        offer3 = side_a.sender.confirm()
        assert offer3 is not None
        wait_until(
            lambda: side_b.store.get_offer(offer3.gift_id) is not None, 10, "B 收到重发"
        )
        assert side_b.recv.accept(offer3.gift_id)
        wait_until(
            lambda: side_a.store.get_offer(offer3.gift_id).state == GiftState.ACCEPTED,
            10, "A 收到重发 accept",
        )
        log.info("PASS 重发并接受成功")

        # 3. 纪念日 +20
        log.info("=== 纪念日特惠 +20 ===")
        set_anniversary_hook(lambda: True)
        before_a, before_b = intimacy(side_a.db), intimacy(side_b.db)
        assert side_a.sender.propose("outfit-anniv-set")
        offer4 = side_a.sender.confirm()
        wait_until(
            lambda: side_b.store.get_offer(offer4.gift_id) is not None, 10, "B 收纪念日礼物"
        )
        assert side_b.recv.accept(offer4.gift_id)
        wait_until(
            lambda: side_a.store.get_offer(offer4.gift_id).state == GiftState.ACCEPTED,
            10, "A 收纪念日 accept",
        )
        wait_until(lambda: intimacy(side_a.db) >= before_a + 20, 5, "A +20")
        wait_until(lambda: intimacy(side_b.db) >= before_b + 20, 5, "B +20")
        log.info(
            "PASS 纪念日 +20（A %s→%s, B %s→%s）",
            before_a, intimacy(side_a.db), before_b, intimacy(side_b.db),
        )
        set_anniversary_hook(lambda: False)

        # 4. 自定义彩蛋
        log.info("=== 自定义彩蛋 ===")
        assert side_a.sender.propose(CUSTOM_EGG_ID, egg_text="今晚一起看星星")
        offer5 = side_a.sender.confirm()
        wait_until(
            lambda: side_b.store.get_offer(offer5.gift_id) is not None, 10, "B 收彩蛋"
        )
        assert side_b.recv.accept(offer5.gift_id)
        wait_until(
            lambda: side_a.store.get_offer(offer5.gift_id).state == GiftState.ACCEPTED,
            10, "A 彩蛋 accept",
        )
        # B 写入记忆库；A 作为发送方 finalize 时也应写入
        wait_until(lambda: side_b.store.egg_count() >= 1, 5, "B 彩蛋入库")
        eggs_b = side_b.store.list_eggs()
        assert any(e.name == "今晚一起看星星" for e in eggs_b)
        log.info("PASS 彩蛋互送（B 记忆库 %s 条）", side_b.store.egg_count())

        # 5. item expire_at 清理
        log.info("=== user_items 到期清理 ===")
        now = int(time.time())
        side_a.store.unlock_item(
            item_id="temp-demo-item", name="临时", item_type="outfit",
            rarity="common", source="gift:demo", expire_at=now - 1,
        )
        removed = side_a.exp.expire_items(now)
        assert "temp-demo-item" in removed
        assert side_a.store.get_item("temp-demo-item") is None
        log.info("PASS 到期物品清理")

        # 取消不发送
        log.info("=== 取消不落库 ===")
        n_before = len(side_a.store.list_offers())
        side_a.sender.propose("emoji-blush")
        side_a.sender.cancel()
        assert len(side_a.store.list_offers()) == n_before
        log.info("PASS 取消不产生记录")

        log.info("=== 全部断言通过 ===")
        log.info(
            "最终亲密度 A=%s B=%s；礼物库 %s 款",
            intimacy(side_a.db), intimacy(side_b.db), len(cfg.catalog),
        )
    finally:
        set_anniversary_hook(lambda: False)
        try:
            side_a.mgr.stop()
        except Exception:
            pass
        try:
            side_b.mgr.stop()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="互赠礼物双端联调")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_demo()


if __name__ == "__main__":
    main()
