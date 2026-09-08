"""共同养成 积分事件接收合入（对应 pet-growth impl §4 / plan S2）。

`PetSync`：接收 `pet.feed` → 直接委托 `apply_intimacy_event`（验签 + 每日上限 +
熔断，唯一入口）→ 成功则回调 `on_intimacy(delta)`。发送侧由 `PetGrowth.award`
承担（同源 apply，两端从同一事件流收敛），本类只管接收侧。

- 载荷校验复用 apply_intimacy_event：delta 非 int/≤0、reason 非法、sig 非 hex
  一律返回 False（丢弃并告警），本类不重复实现。
- `set_mac_key`：换机重配对后更新校验密钥（D22 机制之外的会话变化适配）。
"""

from __future__ import annotations

from typing import Callable

from sync.events import Message

from .consistency import apply_intimacy_event
from .pet_config import PetConfig, load_pet_config


class PetSync:
    """pet.feed 接收合入。发送侧由 PetGrowth.award 承担。"""

    def __init__(
        self,
        db,
        sync,
        mac_key: bytes,
        cfg: PetConfig | None = None,
        on_intimacy: Callable[[int], None] | None = None,
    ) -> None:
        self._db = db
        self._sync = sync  # 保留引用（接口一致性；接收侧当前不直接外发）
        self._mac_key = mac_key
        self._cfg = cfg or load_pet_config()
        self._on_intimacy = on_intimacy

    def handle_pet_feed(self, m: Message) -> bool:
        """处理一条收到的 pet.feed（m.payload 为 impl §1.1 线格式）。

        返回 True=已合入（delta>0，触发 on_intimacy）；False=拒绝（丢弃/告警）。
        """
        if not apply_intimacy_event(self._db, m.payload, self._mac_key):
            return False
        if self._on_intimacy is not None:
            self._on_intimacy(int(m.payload["delta"]))
        return True

    def set_mac_key(self, mac_key: bytes) -> None:
        """换机重配对后更新校验密钥。"""
        self._mac_key = mac_key
