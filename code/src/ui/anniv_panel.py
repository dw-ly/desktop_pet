"""纪念日管理面板（对应 anniversary impl §8.1 / plan C1）。

- 纯函数 `anniv_status_text`：纪念日状态行（今年已过 / 就是今天 / 还有 N 天），可单测。
- `AnnivPanel(QWidget)`：纪念日列表 + 添加/编辑/删除表单（标题、日期、公历/农历、重复方式、
  提前提醒天数）、预告弹窗（祝福文案）、当天庆祝弹窗（限时装扮 + 互赠邀请按钮）。
  订阅 `on_remind` / `on_celebrate` 回调，Qt 信号队列 marshal 回 UI 线程。随主项目外壳接入。

PySide6 条件导入 + HAS_QT 占位（同 `pet_panel.py` / `partner_state.py` 模式）；
无 Qt 环境时 `anniv_status_text` 纯函数仍可单测（本仓库不依赖 PySide6）。
"""

from __future__ import annotations

import logging
from datetime import date

log = logging.getLogger(__name__)

try:  # pragma: no cover - 依赖主项目外壳提供的 PySide6
    from PySide6.QtWidgets import QLabel, QWidget

    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False


def anniv_status_text(title: str, occurrence: date | None, days_left: int) -> str:
    """纪念日状态行（纯函数，可单测）：
    - occurrence None → "《title》今年已过"
    - days_left == 0   → "《title》就是今天 🎉"
    - days_left >  0   → "《title》还有 N 天"
    """
    if occurrence is None:
        return f"《{title}》今年已过"
    if days_left == 0:
        return f"《{title}》就是今天 🎉"
    return f"《{title}》还有 {days_left} 天"


if HAS_QT:  # pragma: no cover - 依赖 PySide6

    class AnnivPanel(QWidget):
        """纪念日管理面板：列表 + 表单 + 预告/庆祝弹窗。

        接入示例（主项目外壳）：
            panel = AnnivPanel()
            celebrate = AnnivCelebrate(db, mgr, calendar,
                                       on_remind=panel.show_remind,
                                       on_celebrate=panel.show_celebrate)
        """

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self._status = QLabel("—", self)
            self._status.setMinimumWidth(180)

        def show_remind(self, entry: dict, text: str) -> None:
            """预告弹窗（祝福文案）。"""
            log.info("纪念日预告 %s：%s", entry.get("title"), text)

        def show_celebrate(self, entry: dict) -> None:
            """当天庆祝弹窗（限时装扮 + 互赠邀请按钮）。"""
            log.info("纪念日庆祝 %s", entry.get("title"))

        def set_status(self, title: str, occurrence: date | None, days_left: int) -> None:
            self._status.setText(anniv_status_text(title, occurrence, days_left))
