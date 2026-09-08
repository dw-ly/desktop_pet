"""当天庆祝与限时装扮（anniversary impl §5 / plan S3）。

- 每日检查：先限时装扮过期回归（D34），再处理每日触发（remind 预告 / celebrate 庆祝）。
- celebrate：解锁限时装扮 → on_celebrate 回调 → 互赠邀请信号（D35）→ 已配对发送 date.remind。
- 接收 date.remind：本端一同庆祝（不反向发送，D38）。
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from typing import Callable

from sync.events import EventType

from .anniv_blessing import AnnivBlessing
from .anniv_calendar import AnnivCalendar
from .anniv_config import AnnivConfig, load_anniv_config

# 互赠邀请钩子（gift-exchange 落地后注册；缺省 no-op，D35）
_GIFT_HOOK: Callable[[dict], None] | None = None


def set_gift_invite_hook(fn: Callable[[dict], None] | None) -> None:
    global _GIFT_HOOK
    _GIFT_HOOK = fn


def _fire_gift_invite(entry: dict) -> None:
    if _GIFT_HOOK is not None:
        _GIFT_HOOK(entry)


def _day_start(d: date) -> int:
    return int(time.mktime((d.year, d.month, d.day, 0, 0, 0, 0, 0, -1)))


class AnnivCelebrate:
    """当天庆祝 + 限时装扮 + date.remind 发送 + 互赠邀请信号。"""

    def __init__(self, db, sync, calendar: AnnivCalendar,
                 cfg: AnnivConfig | None = None,
                 blessing: AnnivBlessing | None = None,
                 on_remind: Callable[[dict, str], None] | None = None,
                 on_celebrate: Callable[[dict], None] | None = None) -> None:
        self._db = db
        self._sync = sync
        self._calendar = calendar
        self._cfg = cfg or load_anniv_config()
        self._blessing = blessing or AnnivBlessing(self._cfg)
        self._on_remind = on_remind
        self._on_celebrate = on_celebrate

    # ------------------------------------------------------------------ #
    # 每日检查
    # ------------------------------------------------------------------ #

    def daily_check(self, today: date | None = None) -> list[str]:
        """处理本日全部待触发项；返回动作列表（处理顺序）。"""
        today = today or date.today()
        actions: list[str] = []
        if self.check_outfit_expiry(today):
            actions.append("outfit_expired")
        for t in self._calendar.daily_triggers(today):
            if t.kind == "remind":
                text = self._blessing.generate(t.entry["title"], today=today)
                if self._on_remind:
                    self._on_remind(t.entry, text)
                self._calendar.mark_triggered(t.entry["id"], "remind", today)
                actions.append(f"remind:{t.entry['id']}")
            else:
                self._celebrate(t.entry, today, send_remind=True)
                self._calendar.mark_triggered(t.entry["id"], "celebrate", today)
                actions.append(f"celebrate:{t.entry['id']}")
        return actions

    # ------------------------------------------------------------------ #
    # 庆祝
    # ------------------------------------------------------------------ #

    def _celebrate(self, entry: dict, today: date, *, send_remind: bool) -> None:
        expire_at = _day_start(today + timedelta(days=self._cfg.outfit_duration_days))
        self._db.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("anniv:limited_outfit",
             json.dumps({"item_id": self._cfg.outfit_item_id, "expire_at": expire_at})),
        )
        if self._on_celebrate:
            self._on_celebrate(entry)
        _fire_gift_invite(entry)
        if send_remind and self._paired():
            self._sync.send(EventType.DATE_REMIND, {
                "id": entry["id"],
                "title": entry["title"],
                "date": entry["date"],
                "calendar": entry["calendar"],
            })

    def handle_date_remind(self, m) -> bool:
        """收到对方 date.remind → 本端一同庆祝（不反向发送）。"""
        p = m.payload
        if not isinstance(p, dict) or not p.get("id") or not p.get("title"):
            return False
        entry = {
            "id": p["id"],
            "title": p["title"],
            "date": p.get("date"),
            "calendar": p.get("calendar", "solar"),
        }
        self._celebrate(entry, date.today(), send_remind=False)
        return True

    # ------------------------------------------------------------------ #
    # 限时装扮（D34）
    # ------------------------------------------------------------------ #

    def check_outfit_expiry(self, today: date | None = None) -> str | None:
        """限时装扮未过期/无 → None；过期 → 清除并返回原 item_id。"""
        today = today or date.today()
        row = self._db.query_one("SELECT value FROM kv WHERE key='anniv:limited_outfit'")
        if not row:
            return None
        data = json.loads(row["value"])
        if today >= date.fromtimestamp(data["expire_at"]):
            self._db.execute("DELETE FROM kv WHERE key='anniv:limited_outfit'")
            return data["item_id"]
        return None

    def current_outfit(self) -> str | None:
        row = self._db.query_one("SELECT value FROM kv WHERE key='anniv:limited_outfit'")
        if not row:
            return None
        return json.loads(row["value"])["item_id"]

    # ------------------------------------------------------------------ #
    # 配对状态（鸭子类型；FakeSync / SyncManager 均可）
    # ------------------------------------------------------------------ #

    def _paired(self) -> bool:
        try:
            return self._sync.pairing_status() == "paired"
        except Exception:  # noqa: BLE001 - 鸭子类型缺省视为未配对（纯本地）
            return False
