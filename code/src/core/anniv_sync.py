"""日历同步与合并（anniversary impl §6 / plan S4）。

- `date.add` 双向同步：增/改/删均在本地落库后向对方发送（未配对不外发）。
- 接收按 LWW（updated_at 最新者胜，D32）：新覆盖 / 旧忽略 / 墓碑删除。
- `date.remind` 归 `AnnivCelebrate` 管辖（D38），本模块只管 date.add。
"""

from __future__ import annotations

import time

from sync.events import EventType

from .anniv_config import AnnivConfig, load_anniv_config
from .anniv_store import AnnivStore


class AnnivSync:
    """date.add 双向同步（LWW）。"""

    def __init__(self, db, sync, store: AnnivStore,
                 cfg: AnnivConfig | None = None) -> None:
        self._db = db
        self._sync = sync
        self._store = store
        self._cfg = cfg or load_anniv_config()

    # ------------------------------------------------------------------ #
    # 本地写 + 发送
    # ------------------------------------------------------------------ #

    def _send_date_add(self, payload: dict) -> None:
        if self._paired():
            self._sync.send(EventType.DATE_ADD, payload)

    def add(self, entry: dict) -> dict:
        d = self._store.add(entry)
        self._send_date_add({**d, "deleted": False})
        return d

    def update(self, anniv_id: str, patch: dict, *, now: int | None = None) -> dict | None:
        d = self._store.update(anniv_id, patch, now=now)
        if d is not None:
            self._send_date_add({**d, "deleted": False})
        return d

    def delete(self, anniv_id: str, *, now: int | None = None) -> bool:
        ok = self._store.delete(anniv_id)
        if ok:
            # 最小墓碑：仅 id/deleted/updated_at（D32）
            self._send_date_add({
                "id": anniv_id,
                "deleted": True,
                "updated_at": now if now is not None else int(time.time()),
            })
        return ok

    # ------------------------------------------------------------------ #
    # 接收合并（LWW）
    # ------------------------------------------------------------------ #

    def handle_date_add(self, m) -> bool:
        p = m.payload
        if not isinstance(p, dict):
            return False
        payload_id = p.get("id")
        updated_at = p.get("updated_at")
        if not isinstance(payload_id, str) or not payload_id:
            return False
        if isinstance(updated_at, bool) or not isinstance(updated_at, int) or updated_at <= 0:
            return False
        local = self._store.get(payload_id)
        if local is not None and local["updated_at"] > updated_at:
            return True  # 旧事件忽略（LWW）
        if p.get("deleted") is True:
            self._store.delete(payload_id)
            return True
        payload = dict(p)
        payload["id"] = payload_id
        payload["updated_at"] = updated_at
        try:
            self._store.upsert(payload)
        except ValueError:
            return False  # 字段非法，不落库
        return True

    # ------------------------------------------------------------------ #
    # 配对状态（鸭子类型）
    # ------------------------------------------------------------------ #

    def _paired(self) -> bool:
        try:
            return self._sync.pairing_status() == "paired"
        except Exception:  # noqa: BLE001 - 鸭子类型缺省视为未配对
            return False
