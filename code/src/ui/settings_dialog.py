"""设置对话框：备份导出/导入 + 手动同步（对应 data-consistency impl §4.6 / plan C1）。

Qt 薄封装：编排逻辑在 `settings_service.py`（本模块只做 PySide6 控件绑定 + 后台
线程执行 + 信号队列 marshal 回 UI 线程）。依赖 PySide6（随主项目外壳提供）；
本仓库未内置 PySide6，模块导入不报错（HAS_QT=False），实例化时提示缺失。

运行前提（主项目外壳）：
    pip install PySide6
    dlg = SettingsDialog(sync, db, mac_key)
    dlg.show()

安全语义：
- 导出/导入口令经 `QLineEdit.Password` 输入，不做日志、不入 keyring、不存盘
  （spec §3.4/§6 决策）；口令遗忘则备份无法恢复。
- 导入前需勾选"确认覆盖"避免误覆盖（plan C1 验收：导入确认避免误覆盖）。
- 耗时操作（导出/导入/手动同步）在后台线程执行，结果经 Qt 信号队列回 UI 线程，
  不阻塞主线程（plan C1 验收：不阻塞主线程）。
"""

from __future__ import annotations

import threading
import logging

log = logging.getLogger(__name__)

try:  # pragma: no cover - 依赖主项目外壳提供的 PySide6
    from PySide6.QtCore import QThread, Signal
    from PySide6.QtWidgets import (
        QCheckBox,
        QDialog,
        QFileDialog,
        QLabel,
        QLineEdit,
        QPushButton,
        QTabWidget,
        QVBoxLayout,
        QWidget,
    )

    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False

from .settings_service import export_backup_flow, import_backup_flow, manual_sync, passphrase_strength


class _TaskThread(QThread):  # pragma: no cover - 依赖 PySide6
    """后台线程执行耗时操作，结果经信号回 UI 线程（Qt 信号队列 marshal）。"""

    ok = Signal(str)
    err = Signal(str)

    def __init__(self, fn, *args) -> None:
        super().__init__()
        self._fn = fn
        self._args = args

    def run(self) -> None:
        try:
            self.ok.emit(str(self._fn(*self._args)))
        except Exception as exc:  # noqa: BLE001 —— UI 层兜底展示错误
            self.err.emit(str(exc))


if HAS_QT:  # pragma: no cover - 依赖 PySide6

    class SettingsDialog(QDialog):
        """备份导出/导入 + 手动同步入口（薄封装，业务逻辑见 settings_service）。"""

        def __init__(self, sync, db, mac_key: bytes | None = None) -> None:
            super().__init__()
            self._sync = sync
            self._db = db
            self._mac_key = mac_key
            self._thread: _TaskThread | None = None
            self.setWindowTitle("设置 · 数据备份与同步")
            self.setMinimumWidth(480)
            self._build_ui()

        # ------------------------------------------------------------------ #
        # UI 构建
        # ------------------------------------------------------------------ #

        def _build_ui(self) -> None:
            tabs = QTabWidget()
            tabs.addTab(self._build_export_tab(), "备份")
            tabs.addTab(self._build_import_tab(), "恢复")

            self._sync_btn = QPushButton("立即同步")
            self._sync_btn.clicked.connect(self._on_manual_sync)

            self._status = QLabel("就绪")
            self._status.setWordWrap(True)

            layout = QVBoxLayout(self)
            layout.addWidget(tabs)
            layout.addWidget(self._sync_btn)
            layout.addWidget(self._status)

        def _build_export_tab(self) -> QWidget:
            page = QWidget()
            v = QVBoxLayout(page)

            v.addWidget(QLabel("备份口令（至少 8 位；遗忘则备份无法恢复，请自行保管）："))
            self._export_pass = QLineEdit()
            self._export_pass.setEchoMode(QLineEdit.Password)
            self._strength = QLabel("强度：弱")
            self._export_pass.textChanged.connect(self._on_pass_changed)
            v.addWidget(self._export_pass)
            v.addWidget(self._strength)

            self._export_path = QLineEdit("backup.tuanzi.bak")
            v.addWidget(QLabel("导出文件："))
            v.addWidget(self._export_path)

            btn = QPushButton("导出备份")
            btn.clicked.connect(self._on_export)
            v.addWidget(btn)
            v.addStretch(1)
            return page

        def _build_import_tab(self) -> QWidget:
            page = QWidget()
            v = QVBoxLayout(page)

            self._import_path = QLineEdit()
            pick = QPushButton("选择备份文件…")
            pick.clicked.connect(self._on_pick_file)
            v.addWidget(QLabel("备份文件："))
            v.addWidget(self._import_path)
            v.addWidget(pick)

            v.addWidget(QLabel("备份口令："))
            self._import_pass = QLineEdit()
            self._import_pass.setEchoMode(QLineEdit.Password)
            v.addWidget(self._import_pass)

            self._confirm = QCheckBox("确认：导入将覆盖当前养成/装扮/纪念日数据")
            v.addWidget(self._confirm)

            btn = QPushButton("导入并恢复")
            btn.clicked.connect(self._on_import)
            v.addWidget(btn)
            v.addStretch(1)
            return page

        # ------------------------------------------------------------------ #
        # 事件处理（后台线程执行 + 信号回 UI 线程）
        # ------------------------------------------------------------------ #

        def _on_pass_changed(self, text: str) -> None:
            self._strength.setText(f"强度：{passphrase_strength(text)}")

        def _on_pick_file(self) -> None:
            path, _ = QFileDialog.getOpenFileName(self, "选择备份文件", "", "团子备份 (*.tuanzi.bak)")
            if path:
                self._import_path.setText(path)

        def _on_export(self) -> None:
            self._run("导出", export_backup_flow, self._db, self._export_pass.text(), self._export_path.text())

        def _on_import(self) -> None:
            if not self._confirm.isChecked():
                self._status.setText("请先勾选确认覆盖再导入")
                return
            self._run("导入", import_backup_flow, self._db, self._import_pass.text(), self._import_path.text(), self._sync)

        def _on_manual_sync(self) -> None:
            self._run("手动同步", manual_sync, self._sync, self._db, self._mac_key)

        def _run(self, kind: str, fn, *args) -> None:
            if self._thread and self._thread.isRunning():
                self._status.setText("已有任务进行中")
                return
            self._status.setText(f"{kind}进行中…")
            self._thread = _TaskThread(fn, *args)
            self._thread.ok.connect(lambda msg: self._status.setText(f"✅ {kind}成功：{msg}"))
            self._thread.err.connect(lambda msg: self._status.setText(f"❌ {kind}失败：{msg}"))
            self._thread.finished.connect(self._thread.deleteLater)
            self._thread.start()

else:  # pragma: no cover - 无 PySide6 时的占位，避免导入崩溃
    class SettingsDialog:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("PySide6 未安装，无法创建设置对话框（随主项目外壳提供）")
