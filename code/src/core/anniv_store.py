"""纪念日数据模型（anniversary impl §2.2 / plan G1）。

`anniversaries` 表 CRUD 唯一入口：字段校验 + 增删改查 + LWW upsert。
- 日期格式：once=`YYYY-MM-DD` / yearly=`MM-DD`（公历/农历同，D31）
- `updated_at` 为 LWW 依据（D32），写入方刷新
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime

from .anniv_config import AnnivConfig, load_anniv_config

DATE_RE_ONCE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATE_RE_YEARLY = re.compile(r"^\d{2}-\d{2}$")
CALENDARS = ("solar", "lunar")
REPEATS = ("once", "yearly")


def _is_real_date(value: str, fmt: str) -> bool:
    try:
        datetime.strptime(value, fmt)
        return True
    except ValueError:
        return False


class AnnivStore:
    """anniversaries 表读写封装 + 字段校验（CRUD 唯一入口）。"""

    def __init__(self, db, cfg: AnnivConfig | None = None) -> None:
        self._db = db
        self._cfg = cfg or load_anniv_config()

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #

    def validate_entry(self, entry: dict) -> dict:
        """字段校验并返回规范 dict（幂等）。非法抛 ValueError。"""
        d = dict(entry)

        anniv_id = d.get("id")
        if anniv_id is None:
            anniv_id = uuid.uuid4().hex
        if not isinstance(anniv_id, str) or not anniv_id:
            raise ValueError("id 必填非空字符串")
        d["id"] = anniv_id

        title = d.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title 必填非空字符串")
        d["title"] = title.strip()

        calendar = d.get("calendar", self._cfg.default_calendar)
        if calendar not in CALENDARS:
            raise ValueError(f"calendar 非法: {calendar!r}")
        d["calendar"] = calendar

        repeat = d.get("repeat", self._cfg.default_repeat)
        if repeat not in REPEATS:
            raise ValueError(f"repeat 非法: {repeat!r}")
        d["repeat"] = repeat

        date_str = d.get("date")
        if not isinstance(date_str, str) or not date_str:
            raise ValueError("date 必填")
        if repeat == "once":
            if not DATE_RE_ONCE.match(date_str) or not _is_real_date(date_str, "%Y-%m-%d"):
                raise ValueError(f"once 日期非法: {date_str!r}（需 YYYY-MM-DD 真实日期）")
        else:
            if not DATE_RE_YEARLY.match(date_str):
                raise ValueError(f"yearly 日期非法: {date_str!r}（需 MM-DD）")
            if not _is_real_date(f"2000-{date_str}", "%Y-%m-%d"):
                raise ValueError(f"yearly 日期非法: {date_str!r}")
        d["date"] = date_str

        notify = d.get("notify_days_before", self._cfg.default_notify_days)
        if isinstance(notify, bool) or not isinstance(notify, int) or notify < 0:
            raise ValueError("notify_days_before 需 int ≥ 0")
        d["notify_days_before"] = notify

        updated_at = d.get("updated_at")
        if updated_at is None:
            updated_at = int(time.time())
        if isinstance(updated_at, bool) or not isinstance(updated_at, int) or updated_at <= 0:
            raise ValueError("updated_at 需 int > 0")
        d["updated_at"] = updated_at

        return d

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #

    def add(self, entry: dict) -> dict:
        """校验 + INSERT；已存在同 id → 抛 ValueError（覆盖走 update/upsert）。"""
        d = self.validate_entry(entry)
        if self._db.query_one("SELECT id FROM anniversaries WHERE id=?", (d["id"],)):
            raise ValueError(f"纪念日已存在: {d['id']}")
        self._db.execute(
            "INSERT INTO anniversaries(id, title, date, repeat, calendar, "
            "notify_days_before, updated_at) VALUES(?,?,?,?,?,?,?)",
            (d["id"], d["title"], d["date"], d["repeat"],
             d["calendar"], d["notify_days_before"], d["updated_at"]),
        )
        return d

    def update(self, anniv_id: str, patch: dict, *, now: int | None = None) -> dict | None:
        """合并 patch 校验后写回并刷新 updated_at；不存在 → None。"""
        cur = self.get(anniv_id)
        if cur is None:
            return None
        merged = dict(cur)
        merged.update(patch)
        merged["id"] = anniv_id
        merged["updated_at"] = now if now is not None else int(time.time())
        d = self.validate_entry(merged)
        self._db.execute(
            "UPDATE anniversaries SET title=?, date=?, repeat=?, calendar=?, "
            "notify_days_before=?, updated_at=? WHERE id=?",
            (d["title"], d["date"], d["repeat"], d["calendar"],
             d["notify_days_before"], d["updated_at"], anniv_id),
        )
        return d

    def upsert(self, entry: dict) -> dict:
        """校验 + INSERT OR REPLACE（同步 LWW 用，按 id 覆盖整行）。"""
        d = self.validate_entry(entry)
        self._db.execute(
            "INSERT INTO anniversaries(id, title, date, repeat, calendar, "
            "notify_days_before, updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET title=excluded.title, date=excluded.date, "
            "repeat=excluded.repeat, calendar=excluded.calendar, "
            "notify_days_before=excluded.notify_days_before, "
            "updated_at=excluded.updated_at",
            (d["id"], d["title"], d["date"], d["repeat"],
             d["calendar"], d["notify_days_before"], d["updated_at"]),
        )
        return d

    def delete(self, anniv_id: str) -> bool:
        cur = self._db.execute(
            "DELETE FROM anniversaries WHERE id=?", (anniv_id,)
        )
        return cur.rowcount > 0

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def get(self, anniv_id: str) -> dict | None:
        row = self._db.query_one(
            "SELECT id, title, date, repeat, calendar, notify_days_before, updated_at "
            "FROM anniversaries WHERE id=?",
            (anniv_id,),
        )
        return self._to_dict(row) if row else None

    def list_all(self) -> list[dict]:
        """全量，按 updated_at 升序（稳定序，测试可断言）。"""
        rows = self._db.query_all(
            "SELECT id, title, date, repeat, calendar, notify_days_before, updated_at "
            "FROM anniversaries ORDER BY updated_at ASC"
        )
        return [self._to_dict(r) for r in rows]

    @staticmethod
    def _to_dict(row) -> dict:
        return {
            "id": row["id"],
            "title": row["title"],
            "date": row["date"],
            "repeat": row["repeat"],
            "calendar": row["calendar"],
            "notify_days_before": row["notify_days_before"],
            "updated_at": row["updated_at"],
        }
