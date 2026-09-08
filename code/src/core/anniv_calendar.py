"""农历换算与每日触发扫描（anniversary impl §3 / plan S1）。

- `lunar_to_solar`：农历 → 公历（lunarcalendar 封装，含闰月，1900-2099）。
- `occurrence_date` / `next_occurrence`：纪念日在指定年份/下一个未来发生日（D37 跨年提醒）。
- `AnnivCalendar.daily_triggers`：每日待触发列表（celebrate / remind），已标记跳过（D33）。
- `is_anniversary`：任一纪念日 today 为发生日（S5 钩子同源判定）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .anniv_config import AnnivConfig, load_anniv_config
from .anniv_store import AnnivStore

_LUNAR_MIN_YEAR = 1900
_LUNAR_MAX_YEAR = 2099


def lunar_to_solar(year: int, month: int, day: int) -> date:
    """农历 → 公历（含闰月）。year 越界 / 月日非法 → ValueError；库缺失 → ImportError。"""
    if not (_LUNAR_MIN_YEAR <= year <= _LUNAR_MAX_YEAR):
        raise ValueError(f"农历年份越界: {year}（支持 {_LUNAR_MIN_YEAR}-{_LUNAR_MAX_YEAR}）")
    if not (1 <= month <= 12) or not (1 <= day <= 30):
        raise ValueError(f"农历日期非法: {year}-{month}-{day}")
    try:
        from lunarcalendar import Converter, Lunar  # 延迟导入：未装库时仅农历路径报错
    except ImportError:
        raise ImportError("lunarcalendar 未安装，无法换算农历") from None
    try:
        solar = Converter.Lunar2Solar(Lunar(year, month, day))
    except Exception as exc:  # 小月无 30 等边界
        raise ValueError(f"农历日期换算失败: {year}-{month}-{day}") from exc
    return date(solar.year, solar.month, solar.day)


def occurrence_date(entry: dict, year: int) -> date | None:
    """该纪念日在指定年份的公历发生日（lunar 先换算；once 年份不匹配 → None）。"""
    parts = entry["date"].split("-")
    if entry["repeat"] == "once":
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        if y != year:
            return None
        if entry["calendar"] == "lunar":
            return lunar_to_solar(y, m, d)
        return date(y, m, d)
    # yearly：当年发生日（lunar 换算可能落入 year+1，属正常）
    m, d = int(parts[0]), int(parts[1])
    if entry["calendar"] == "lunar":
        return lunar_to_solar(year, m, d)
    return date(year, m, d)


@dataclass(frozen=True)
class AnnivTrigger:
    entry: dict
    kind: str          # 'remind' | 'celebrate'
    occurrence: date   # 本次触发对应公历日
    distance: int      # (occurrence - today).days


class AnnivCalendar:
    """每日触发扫描（纯查询 + 标记读；标记写由处理方 mark_triggered 负责）。"""

    def __init__(self, db, cfg: AnnivConfig | None = None) -> None:
        self._db = db
        self._cfg = cfg or load_anniv_config()
        self._store = AnnivStore(db, cfg)

    # ------------------------------------------------------------------ #
    # 发生日
    # ------------------------------------------------------------------ #

    def next_occurrence(self, entry: dict, today: date) -> date | None:
        """下一个未来（≥today）发生日；once 已过 / 永不 → None（D37）。"""
        occ = occurrence_date(entry, today.year)
        if occ is None:
            return None
        if occ < today:
            if entry["repeat"] == "once":
                return None
            occ = occurrence_date(entry, today.year + 1)
            if occ is None:
                return None
        return occ

    # ------------------------------------------------------------------ #
    # 每日扫描
    # ------------------------------------------------------------------ #

    def daily_triggers(self, today: date | None = None) -> list[AnnivTrigger]:
        """本日待触发列表：distance==0 → celebrate；0<distance==notify → remind。
        已标记 kv['anniv:triggered:<iso>:<id>:<kind>'] → 跳过（D33）。按 id 稳定排序。"""
        today = today or date.today()
        out: list[AnnivTrigger] = []
        for entry in self._store.list_all():
            occ = self.next_occurrence(entry, today)
            if occ is None:
                continue
            distance = (occ - today).days
            if distance == 0:
                kind = "celebrate"
            elif distance == entry["notify_days_before"] and distance > 0:
                kind = "remind"
            else:
                continue
            if self._has_marker(entry["id"], kind, today):
                continue
            out.append(AnnivTrigger(entry=entry, kind=kind, occurrence=occ, distance=distance))
        out.sort(key=lambda t: t.entry["id"])
        return out

    def is_anniversary(self, today: date | None = None) -> bool:
        """任一纪念日 today 为发生日（与 celebrate 判定同源）。"""
        today = today or date.today()
        return any(self.next_occurrence(e, today) == today for e in self._store.list_all())

    # ------------------------------------------------------------------ #
    # 触发标记（D33）
    # ------------------------------------------------------------------ #

    def mark_triggered(self, anniv_id: str, kind: str, today: date) -> None:
        key = f"anniv:triggered:{today.isoformat()}:{anniv_id}:{kind}"
        self._db.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, "1"),
        )

    def _has_marker(self, anniv_id: str, kind: str, today: date) -> bool:
        key = f"anniv:triggered:{today.isoformat()}:{anniv_id}:{kind}"
        return self._db.query_one("SELECT value FROM kv WHERE key=?", (key,)) is not None
