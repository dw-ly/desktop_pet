"""宠物名同步与形象选择测试（pet-growth impl §7.2 / plan S5）。"""

from __future__ import annotations

import json

import pytest

from core.db import init_core
from core.pet_profile import PetProfile
from sync.events import EventType, Message


class FakeSync:
    def __init__(self, partner_id: str | None = "peer-b") -> None:
        self._partner = partner_id
        self.calls: list[tuple[EventType, dict]] = []

    def partner_id(self) -> str | None:
        return self._partner

    def send(self, type_, payload: dict, **kwargs) -> None:
        self.calls.append((type_, payload))


def _msg(payload: dict, from_id: str = "peer-b") -> Message:
    return Message(v=1, type="pet.profile", from_id=from_id, seq=1, ts=1786000000,
                   payload=payload)


def _write_manifest(assets: str, preset_id: str, name: str) -> None:
    import pathlib
    d = pathlib.Path(assets) / "pets" / preset_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(
        json.dumps({"version": 1, "id": preset_id, "name": name,
                    "states": {"idle": f"assets/pets/{preset_id}/idle.svg"}},
                   ensure_ascii=False),
        encoding="utf-8",
    )


def test_set_name_persists(tmp_path) -> None:
    db = init_core(tmp_path)
    pp = PetProfile(db, FakeSync(partner_id=None))
    pp.set_my_pet_name("小团子")
    assert pp.my_pet_name() == "小团子"
    db.close()


def test_rename_resends_when_paired(tmp_path) -> None:
    db = init_core(tmp_path)
    sync = FakeSync(partner_id="peer-b")
    pp = PetProfile(db, sync)
    pp.set_my_pet_name("小团子")
    assert sync.calls == [(EventType.PET_PROFILE, {"name": "小团子"})]
    pp.set_my_pet_name("大团子")  # 改名重发
    assert sync.calls[-1] == (EventType.PET_PROFILE, {"name": "大团子"})
    db.close()


def test_no_send_when_unpaired(tmp_path) -> None:
    db = init_core(tmp_path)
    sync = FakeSync(partner_id=None)
    pp = PetProfile(db, sync)
    pp.set_my_pet_name("小团子")
    pp.send_profile()
    assert sync.calls == []  # 未配对不发送
    db.close()


def test_send_profile_when_paired(tmp_path) -> None:
    db = init_core(tmp_path)
    sync = FakeSync(partner_id="peer-b")
    pp = PetProfile(db, sync)
    pp.set_my_pet_name("小团子")
    sync.calls.clear()
    pp.send_profile()  # 配对成功后补发
    assert sync.calls == [(EventType.PET_PROFILE, {"name": "小团子"})]
    db.close()


def test_receive_stores_peer_name(tmp_path) -> None:
    db = init_core(tmp_path)
    pp = PetProfile(db, FakeSync(partner_id="peer-b"))
    assert pp.handle_pet_profile(_msg({"name": "TA 的团子"})) is True
    assert pp.peer_pet_name() == "TA 的团子"
    db.close()


def test_receive_rejects_malformed(tmp_path) -> None:
    db = init_core(tmp_path)
    pp = PetProfile(db, FakeSync(partner_id="peer-b"))
    assert pp.handle_pet_profile(_msg({})) is False
    assert pp.handle_pet_profile(_msg({"name": 123})) is False
    assert pp.peer_pet_name() is None
    db.close()


def test_list_presets(tmp_path) -> None:
    db = init_core(tmp_path)
    assets = tmp_path / "assets"
    _write_manifest(str(assets), "line-dog-2", "线条小狗·款二")
    _write_manifest(str(assets), "moon-cat", "月薪猫")
    _write_manifest(str(assets), "line-dog-1", "线条小狗·款一")
    pp = PetProfile(db, FakeSync(), assets_dir=assets)
    presets = pp.list_presets()
    assert [p["id"] for p in presets] == ["line-dog-1", "line-dog-2", "moon-cat"]
    assert presets[0]["name"] == "线条小狗·款一"
    assert "idle" in presets[0]["states"]
    db.close()


def test_missing_assets_fallback(tmp_path) -> None:
    db = init_core(tmp_path)
    pp = PetProfile(db, FakeSync(), assets_dir=tmp_path / "empty")
    assert pp.list_presets() == []
    with pytest.raises(ValueError):
        pp.set_preset("nope")  # 未知预设抛 ValueError
    db.close()


def test_preset_local_only(tmp_path) -> None:
    db = init_core(tmp_path)
    assets = tmp_path / "assets"
    _write_manifest(str(assets), "moon-cat", "月薪猫")
    sync = FakeSync(partner_id="peer-b")
    pp = PetProfile(db, sync, assets_dir=assets)
    pp.set_preset("moon-cat")
    assert pp.current_preset() == "moon-cat"
    assert sync.calls == []  # 形象不同步（spec §3.3.7）
    db.close()
