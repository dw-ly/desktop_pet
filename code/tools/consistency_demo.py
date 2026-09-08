"""数据一致性双端联调脚本（对应 data-consistency impl §4.6 / plan C2）。

一键验证全链路：配对 → 在线互发(pet.feed) → 去重 → 断网离线入队 → 重连按序补发 →
双端积分重放收敛 → 清理(prune) → 每日对齐(daily_align) → 导出备份 →
模拟换机（新 data_dir + 重新配对）→ 导入恢复 → 校验数据完整。

全程断言：无重复投递、补发按 seq 升序、双端 events 表事件集一致 → 重放收敛、
daily_align 兜底修复、prune 保留 pet.feed 完整历史、备份恢复后数据完整。

用法（在 code 目录下）：
    .venv\\Scripts\\python tools\\consistency_demo.py            # 常规（INFO）
    .venv\\Scripts\\python tools\\consistency_demo.py --debug   # 含帧级日志
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
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core import (  # noqa: E402
    AlignResult,
    apply_intimacy_event,
    daily_align,
    export_backup,
    handle_peer_snapshot,
    import_backup,
    init_core,
    replay_intimacy_total,
    sign_intimacy_event,
)
from core.prune import prune_event_log  # noqa: E402
from sync import EventType, SyncConfig, SyncManager  # noqa: E402
from sync.crypto import derive_mac_key, format_code, generate_identity  # noqa: E402
from sync.events import Message  # noqa: E402
from sync.key_store import KeyStore  # noqa: E402
from sync.manager import PairingEvent  # noqa: E402
from sync.queue import SyncQueue  # noqa: E402
from sync.transport import ConnState  # noqa: E402

log = logging.getLogger("consistency_demo")

PASS = "换机口令需足够长且含多类字符!1"
DAY = 86400


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

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
    ks.delete_identity()  # 清 keyring/文件残留，确保 manager 加载本文件
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


def feed(mgr, db, mac_key: bytes, delta: int, reason: str = "feed") -> bool:
    """本端喂食：签名并发送 pet.feed + 本端合入（本地喂食不受同步状态影响）。"""
    ts = int(time.time())
    payload = {
        "delta": delta,
        "reason": reason,
        "ts": ts,
        "sig": sign_intimacy_event(mac_key, delta, reason, ts),
    }
    mgr.send(EventType.PET_FEED, payload)
    return apply_intimacy_event(db, payload, mac_key)


def _feed_count(db) -> int:
    return db.query_one(
        "SELECT COUNT(*) AS n FROM events WHERE type='pet.feed'"
    )["n"]


# --------------------------------------------------------------------------- #
# 一端装配
# --------------------------------------------------------------------------- #

def make_side(data_dir: str, label: str, mac_key: bytes, port_range: tuple,
              on_pairing) -> SimpleNamespace:
    cfg = SyncConfig(data_dir=data_dir, instance_id=label, ws_port_range=port_range)
    db = init_core(data_dir)
    received_feed: list[Message] = []
    # 校验密钥放可变容器：换机重配对后对方身份变化，pet.feed 校验密钥随之更新
    mac_key_box = {"key": mac_key}

    def on_pet_feed(m: Message) -> None:
        received_feed.append(m)
        applied = apply_intimacy_event(db, m.payload, mac_key_box["key"])
        log.info("[%s] 收到 pet.feed delta=%s → %s", label, m.payload.get("delta"),
                 "合入" if applied else "拒绝")

    def on_date_sync(m: Message) -> None:
        res = handle_peer_snapshot(db, m.payload)
        log.info("[%s] 收到 date.sync streak=%s → %s", label, m.payload.get("streak"), res.value)

    mgr = SyncManager(
        cfg,
        db=db,
        on_state=lambda s: log.info("[%s] 连接状态=%s", label, s.value),
        on_pairing=on_pairing,
    )
    mgr.add_handler(EventType.PET_FEED, on_pet_feed)
    mgr.add_handler(EventType.DATE_SYNC, on_date_sync)
    return SimpleNamespace(mgr=mgr, db=db, received_feed=received_feed,
                           mac_key_box=mac_key_box)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def run_demo(port_range=(48000, 48099)) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tuanzi-consistency-demo-"))
    dir_a, dir_b, dir_a2 = str(tmp / "a"), str(tmp / "b"), str(tmp / "a2")
    bak = tmp / "backup.tuanzi.bak"

    # 预置双端身份（换机时 A2 换新身份、B 保持）
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

    side_a = make_side(dir_a, "consA", mac_key, port_range, on_pairing_a)
    side_b = make_side(dir_b, "consB", mac_key, port_range, on_pairing_b)

    try:
        # 初始数据：A 共同天数 3，B 共同天数 1（换机后校验只增兜底）
        side_a.db.execute("INSERT INTO pet_state(key, value) VALUES('level', 5), ('streak_days', 3)")
        side_b.db.execute("INSERT INTO pet_state(key, value) VALUES('level', 5), ('streak_days', 1)")

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

        # 2. 在线互发 pet.feed（A 2 条 + B 1 条；本端合入 + 对方合入）
        log.info("=== 在线互发 pet.feed ===")
        assert feed(side_a.mgr, side_a.db, mac_key, 2)
        assert feed(side_a.mgr, side_a.db, mac_key, 3)
        assert feed(side_b.mgr, side_b.db, mac_key, 4)
        wait_until(lambda: len(side_b.received_feed) >= 2, 10, "B 收到 A 在线 2 条")
        wait_until(lambda: len(side_a.received_feed) >= 1, 10, "A 收到 B 在线 1 条")
        log.info("✅ 在线互发成功")

        # 3. 去重：构造重复事件 → receive 丢弃，行数不变
        log.info("=== 去重 ===")
        a_peer = side_a.mgr.peer_id()
        b_peer = side_b.mgr.peer_id()
        row = side_b.db.query_one(
            "SELECT seq, payload_json FROM events WHERE peer=? AND type='pet.feed' ORDER BY seq LIMIT 1",
            (a_peer,),
        )
        dup = Message(v=1, type="pet.feed", from_id=a_peer, seq=row["seq"],
                      ts=int(time.time()), payload=json.loads(row["payload_json"]))
        n_before = _feed_count(side_b.db)
        assert SyncQueue(side_b.db, b_peer).receive(dup, lambda m: None) is False
        assert _feed_count(side_b.db) == n_before
        log.info("✅ 去重：重复 event_id 丢弃，未重复投递")

        # 4. 断网离线入队
        log.info("=== 断网离线入队 ===")
        side_b.mgr.stop()
        wait_until(
            lambda: side_a.mgr.connection_status() != ConnState.CONNECTED,
            timeout=10, desc="A 检测到 B 离线",
        )
        assert feed(side_a.mgr, side_a.db, mac_key, 1)
        assert feed(side_a.mgr, side_a.db, mac_key, 1)
        assert side_a.db.query_one(
            "SELECT COUNT(*) AS n FROM events WHERE status='pending' AND type='pet.feed'"
        )["n"] == 2
        log.info("✅ 离线入队：A 2 条 pet.feed 进入 pending（本端亲密度已合入）")

        # 5. 重连补发（按 seq 升序、无重复）
        log.info("=== 重连补发 ===")
        side_b.mgr.start()  # 复用同一 db（manager 停止时未关闭）
        wait_until(lambda: len(side_b.received_feed) >= 4, 20, "B 补齐离线 2 条")
        seqs = [m.seq for m in side_b.received_feed]
        assert seqs == sorted(seqs), f"补发未按 seq 升序: {seqs}"
        assert len(seqs) == len(set(seqs)), "补发存在重复"
        assert side_a.db.query_one(
            "SELECT COUNT(*) AS n FROM events WHERE status='pending' AND type='pet.feed'"
        )["n"] == 0
        log.info("✅ 重连补发成功（按 seq 升序、无重复）")

        # 6. 双端事件集一致 + 亲密度重放收敛
        log.info("=== 重放收敛 ===")
        assert _feed_count(side_a.db) == 5, f"A 端 events 应 5 条: {_feed_count(side_a.db)}"
        assert _feed_count(side_b.db) == 5, f"B 端 events 应 5 条: {_feed_count(side_b.db)}"
        assert _intimacy(side_a.db) == 11 and _intimacy(side_b.db) == 11
        ra = replay_intimacy_total(side_a.db, mac_key)
        rb = replay_intimacy_total(side_b.db, mac_key)
        assert ra == rb == 11, f"重放应收敛到 11: A={ra} B={rb}"
        log.info("✅ 双端重放收敛：pet_state.intimacy == 重放总和 == 11")

        # 6b. 兜底修复：篡改 A 亲密度 → daily_align 修复为重放总和
        log.info("=== 兜底修复 ===")
        side_a.db.execute("UPDATE pet_state SET value=0 WHERE key='intimacy'")
        assert daily_align(side_a.mgr, side_a.db, mac_key) is AlignResult.REPAIRED
        assert _intimacy(side_a.db) == 11
        log.info("✅ daily_align 兜底修复：偏差亲密度 → 重放总和")

        # 7. 清理：超期普通事件删除 + pet.feed 完整历史保留（D17）
        log.info("=== 清理(prune) ===")
        old = int(time.time()) - 100 * DAY
        side_a.db.execute(
            "INSERT INTO events(event_id, seq, type, peer, payload_json, created_at, status) "
            "VALUES('OLD:1', NULL, 'msg.carry', 'B', '{}', ?, 'received')", (old,))
        side_a.db.execute(
            "INSERT INTO events(event_id, seq, type, peer, payload_json, created_at, status) "
            "VALUES('OLD:2', NULL, 'pet.feed', 'B', '{}', ?, 'received')", (old,))
        deleted = prune_event_log(side_a.db)
        assert deleted == 1, f"应只删普通超期事件: {deleted}"
        assert side_a.db.query_one("SELECT 1 FROM events WHERE event_id='OLD:1'") is None
        assert side_a.db.query_one("SELECT 1 FROM events WHERE event_id='OLD:2'") is not None
        log.info("✅ 清理：超期普通事件删除，pet.feed 完整历史保留")

        # 8. 每日对齐正常态：双端已收敛 → OK，date.sync 快照已交换
        log.info("=== 每日对齐 ===")
        assert daily_align(side_a.mgr, side_a.db, mac_key) is AlignResult.OK
        wait_until(lambda: _streak(side_b.db) >= 3, 10, "B 采纳 A 的 streak(只增)")
        log.info("✅ 每日对齐 OK：streak 只增兜底生效（B:1 → 3）")

        # 9. 导出备份（含装扮/纪念日）
        log.info("=== 导出备份 ===")
        side_a.db.execute(
            "INSERT INTO user_items(item_id, name, type, rarity, source, expire_at) "
            "VALUES('i1', '爱心气泡', 'costume', 'rare', 'B', NULL)")
        side_a.db.execute(
            "INSERT INTO anniversaries(id, title, date, repeat, calendar, notify_days_before, updated_at) "
            "VALUES('a1', '在一起', '2026-08-05', 'yearly', 'solar', 3, 1783000000)")
        n_export = export_backup(side_a.db, PASS, bak)
        assert n_export == 5  # pet_state(level/intimacy/streak_days) 3 + user_items 1 + anniversaries 1
        snap = {
            "pet_state": [dict(r) for r in side_a.db.query_all("SELECT * FROM pet_state")],
            "user_items": [dict(r) for r in side_a.db.query_all("SELECT * FROM user_items")],
            "anniversaries": [dict(r) for r in side_a.db.query_all("SELECT * FROM anniversaries")],
        }
        log.info("✅ 导出备份：%s 行 → %s", n_export, bak.name)

        # 10. 模拟换机：停双端 → 新设备 A2 导入备份 → 校验数据完整
        log.info("=== 换机恢复 ===")
        side_a.mgr.stop()
        side_b.mgr.stop()
        db_a2 = init_core(dir_a2)
        n_import = import_backup(db_a2, PASS, bak)
        assert n_import == n_export
        got = {
            "pet_state": [dict(r) for r in db_a2.query_all("SELECT * FROM pet_state")],
            "user_items": [dict(r) for r in db_a2.query_all("SELECT * FROM user_items")],
            "anniversaries": [dict(r) for r in db_a2.query_all("SELECT * FROM anniversaries")],
        }
        assert got == snap, "A2 三表应与 A 备份快照一致"
        assert db_a2.query_one("SELECT COUNT(*) AS n FROM events")["n"] == 0  # 导入不触碰 events
        log.info("✅ 导入恢复：三表与备份快照一致，events 未被触碰")

        # 11. B 重启解除旧配对 → A2 重新配对（换机重配对）
        log.info("=== 重新配对 ===")
        side_b.mgr.start()
        side_b.mgr.revoke_pairing()
        wait_until(lambda: side_b.mgr.pairing_status() == "unpaired", 10, "B 解除旧配对")

        a2_ready = threading.Event()
        mgr_a2 = None

        def on_pairing_a2(e: PairingEvent, d: dict) -> None:
            log.info("[A2] 配对事件=%s %s", e.value, d)
            if e == PairingEvent.PEER_REQUEST:
                threading.Thread(
                    target=lambda: mgr_a2.confirm_peer(d["peer_id"]), daemon=True
                ).start()
            if e == PairingEvent.PAIRED:
                a2_ready.set()

        cfg_a2 = SyncConfig(data_dir=dir_a2, instance_id="consA2", ws_port_range=port_range)
        mgr_a2 = SyncManager(
            cfg_a2,
            db=db_a2,
            on_state=lambda s: log.info("[A2] 连接状态=%s", s.value),
            on_pairing=on_pairing_a2,
        )
        mgr_a2.start()
        # A2 身份由 manager 生成；读回私钥派生新会话 MAC 键（前向同步用）
        a2_priv = KeyStore(dir_a2).load_identity()
        mac_key2 = derive_mac_key(a2_priv, b_kp.public_key)
        a2_feed_in: list[Message] = []

        def on_feed_a2(m: Message) -> None:
            a2_feed_in.append(m)
            applied = apply_intimacy_event(db_a2, m.payload, mac_key2)
            log.info("[A2] 收到 pet.feed delta=%s → %s", m.payload.get("delta"),
                     "合入" if applied else "拒绝")

        mgr_a2.add_handler(EventType.PET_FEED, on_feed_a2)
        mgr_a2.add_handler(EventType.DATE_SYNC, lambda m: handle_peer_snapshot(db_a2, m.payload))

        code2 = mgr_a2.start_pairing()
        log.info("[A2] 配对码: %s", format_code(code2))
        side_b.mgr.confirm_pairing(code2)
        wait_until(lambda: a2_ready.is_set(), 15, "A2 完成配对")
        # B 端校验密钥切到新会话（A2 换新身份 → mac_key2），后续 pet.feed 才可通过签名校验
        side_b.mac_key_box["key"] = mac_key2
        log.info("✅ 重新配对成功（A2 换新身份 + B）")

        # 12. 恢复后对齐：A2 发 date.sync 快照（备份快照为准，不做重放修复）
        log.info("=== 恢复后对齐 ===")
        wait_until(
            lambda: mgr_a2.connection_status().value == "connected"
            and side_b.mgr.connection_status().value == "connected",
            15, "A2/B 传输已连接（对齐前）",
        )
        assert daily_align(mgr_a2, db_a2, mac_key=None) is AlignResult.OK
        assert _intimacy(db_a2) == 11, "恢复后亲密度以备份快照为准（不被清空）"
        wait_until(lambda: _streak(side_b.db) >= 3, 10, "B 采纳 A2 的 streak")
        assert _streak(side_b.db) == 3
        log.info("✅ 恢复后对齐：备份快照为准，streak 只增兜底再次生效")

        # 13. 前向同步：A2 新会话事件 → B 合入。
        # 注意：B 今日 feed 已 5 次达日上限（会话1 遗留，防作弊正确拦截第 6 条），
        # 故改用 gift（发送侧限次、接收侧无每日上限）验证换机后跨会话传播/合入。
        log.info("=== 前向同步 ===")
        assert feed(mgr_a2, db_a2, mac_key2, 2, reason="gift")
        wait_until(lambda: _intimacy(side_b.db) == 13, 10, "B 合入 A2 新事件")
        assert _intimacy(db_a2) == 13  # 备份 11 + 新事件 2
        log.info("✅ 前向同步：新会话事件合入（A2/B 亲密度 → 13）")

        log.info("=== 全部通过 ===")
    finally:
        for m in (side_a, side_b):
            if m and m.mgr:
                try:
                    m.mgr.stop()
                except Exception:  # noqa: BLE001
                    pass
        if "mgr_a2" in dir():
            try:
                mgr_a2.stop()
            except Exception:  # noqa: BLE001
                pass
        for d in [side_a.db if side_a else None, side_b.db if side_b else None]:
            if d:
                try:
                    d.close()
                except Exception:  # noqa: BLE001
                    pass
        if "db_a2" in dir():
            try:
                db_a2.close()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="数据一致性双端联调脚本")
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
