"""共同养成 宠物名同步与形象选择（对应 pet-growth impl §7 / plan S5）。

- 宠物名：本端名存 kv['pet:my_name']；配对后经 `pet.profile` 加密同步，对方存
  `pairing.peer_pet_name` 供状态栏显示"TA 的团子：××"；改名后重新发送（spec
  §3.3.7）。
- 形象：读取 `assets/pets/<id>/manifest.json` 构建预设列表（3 款：月薪猫、线条
  小狗 ×2），按清单加载素材、缺失兜底默认形象；形象配置仅本端生效不同步。
- manifest 结构（impl §7.1 定稿）：{version, id, name, states:{idle: 路径}}。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from sync.events import EventType, Message

from .pet_config import PetConfig, load_pet_config

_KV_MY_NAME = "pet:my_name"
_KV_PRESET = "pet:preset_id"

# 默认素材包目录：code/assets（pet_profile.py 位于 code/src/core/ 三级向上）
_DEFAULT_ASSETS = Path(__file__).resolve().parents[2] / "assets"


def _kv_get(db, key: str) -> str | None:
    row = db.query_one("SELECT value FROM kv WHERE key=?", (key,))
    return str(row["value"]) if row else None


def _kv_set(db, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


class PetProfile:
    """宠物名同步（pet.profile）+ 形象预设选择（assets/pets manifest）。"""

    def __init__(
        self,
        db,
        sync,
        cfg: PetConfig | None = None,
        assets_dir: str | Path | None = None,
    ) -> None:
        self._db = db
        self._sync = sync  # SyncManager 鸭子类型（partner_id/send）
        self._cfg = cfg or load_pet_config()
        self._assets_dir = Path(assets_dir) if assets_dir else _DEFAULT_ASSETS

    # ------------------------------------------------------------------ #
    # 宠物名
    # ------------------------------------------------------------------ #

    def set_my_pet_name(self, name: str) -> None:
        """写本端宠物名；已配对则发送 pet.profile（改名重发）。空串忽略。"""
        if not isinstance(name, str) or not name.strip():
            return
        _kv_set(self._db, _KV_MY_NAME, name.strip())
        if self._sync.partner_id() is not None:
            self._sync.send(EventType.PET_PROFILE, {"name": name.strip()})

    def my_pet_name(self) -> str:
        return _kv_get(self._db, _KV_MY_NAME) or ""

    def peer_pet_name(self) -> str | None:
        """pairing.peer_pet_name（pet.profile 写入）；无 → None。"""
        partner = self._sync.partner_id()
        if partner is None:
            return None
        row = self._db.query_one(
            "SELECT peer_pet_name FROM pairing WHERE pairing_id=?",
            (partner,),
        )
        return row["peer_pet_name"] if row else None

    def handle_pet_profile(self, m: Message) -> bool:
        """接收 pet.profile → upsert pairing(peer_pet_name)。非法负载 → False。"""
        name = m.payload.get("name")
        if not isinstance(name, str) or not name.strip():
            return False
        self._db.execute(
            "INSERT INTO pairing(pairing_id, peer_id, peer_pet_name, created_at) "
            "VALUES(?, ?, ?, ?) "
            "ON CONFLICT(pairing_id) DO UPDATE SET "
            "peer_id=excluded.peer_id, peer_pet_name=excluded.peer_pet_name, "
            "created_at=excluded.created_at",
            (m.from_id, m.from_id, name.strip(), int(time.time())),
        )
        return True

    def send_profile(self) -> None:
        """配对成功后发送本端宠物名（无名字时不发）。"""
        name = self.my_pet_name()
        if name and self._sync.partner_id() is not None:
            self._sync.send(EventType.PET_PROFILE, {"name": name})

    # ------------------------------------------------------------------ #
    # 形象预设
    # ------------------------------------------------------------------ #

    def list_presets(self) -> list[dict]:
        """扫描 assets/pets/*/manifest.json，按 id 排序返回 [{id, name, states}]。"""
        pets_dir = self._assets_dir / "pets"
        presets: list[dict] = []
        if not pets_dir.is_dir():
            return presets
        for manifest in sorted(pets_dir.glob("*/manifest.json")):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue  # 缺失/损坏清单跳过
            if not isinstance(data, dict) or "id" not in data or "name" not in data:
                continue
            presets.append(
                {
                    "id": str(data["id"]),
                    "name": str(data["name"]),
                    "states": data.get("states", {}),
                }
            )
        presets.sort(key=lambda p: p["id"])
        return presets

    def current_preset(self) -> str | None:
        return _kv_get(self._db, _KV_PRESET)

    def set_preset(self, preset_id: str) -> None:
        """写 kv['pet:preset_id']（仅本端生效，不同步）；preset_id 非法抛 ValueError。"""
        known = {p["id"] for p in self.list_presets()}
        if preset_id not in known:
            raise ValueError(f"未知形象预设: {preset_id!r}")
        _kv_set(self._db, _KV_PRESET, preset_id)
