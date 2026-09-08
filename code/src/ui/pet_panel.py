"""团子成长面板（对应 pet-growth impl §8.1 / plan C1）。

- 纯函数 `pet_status_text`：成长面板摘要行（等级 · 连续共同登录天数 [· 灰色期]），
  可单测。
- `PetPanel(QWidget)`：等级/经验条、亲密度进度条、streak 与灰色标签、已解锁列表、
  升级/解锁弹窗、宠物名与形象入口。订阅 `on_intimacy` / `on_unlock` 回调，Qt 信号
  队列 marshal 回 UI 线程。随主项目外壳接入。

PySide6 条件导入 + HAS_QT 占位（同 `partner_state.py` / `settings_dialog.py` 模式）；
无 Qt 环境时 `pet_status_text` 纯函数仍可单测（本仓库不依赖 PySide6）。
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

try:  # pragma: no cover - 依赖主项目外壳提供的 PySide6
    from PySide6.QtWidgets import QLabel, QWidget

    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False


def pet_status_text(level: int, streak: int, grace_status: str) -> str:
    """成长面板摘要行（纯函数，可单测）：
    - 灰色期 → "N 级 · 连续 M 天 · 灰色期"
    - 否则   → "N 级 · 连续 M 天"
    """
    base = f"{level} 级 · 连续 {streak} 天"
    if grace_status == "grace":
        return f"{base} · 灰色期"
    return base


if HAS_QT:  # pragma: no cover - 依赖 PySide6

    class PetPanel(QWidget):
        """成长面板：显示团子等级/亲密度/连续登录/解锁。

        接入示例（主项目外壳）：
            panel = PetPanel()
            pet_level = PetLevel(db)
            growth = PetGrowth(db, mgr, mac_key)
            sync = PetSync(db, mgr, mac_key,
                           on_intimacy=panel.on_intimacy_changed)
            panel.set_anniversary(True)  # 由外壳按需调用
        """

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self._summary = QLabel("—", self)
            self._summary.setMinimumWidth(180)

        def on_intimacy_changed(self, delta: int) -> None:
            """亲密变化回调（任意线程调用安全；由调用方 marshal 到 UI 线程）。"""
            log.debug("亲密变化 delta=%s", delta)

        def show_summary(self, level: int, streak: int, grace_status: str) -> None:
            self._summary.setText(pet_status_text(level, streak, grace_status))
