"""设置/备份 UI 层（对应 data-consistency impl §4.6 / plan C1）。

`settings_dialog.py` 为 PySide6 薄封装，本包其余部分为 UI 无关代码，避免在
未安装 Qt 的环境导入失败。具体编排逻辑在 `settings_service.py`。
"""
