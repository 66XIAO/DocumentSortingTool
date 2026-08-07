"""启动自检：走 app.main 的真实装配路径，构造主窗口后立刻退出。

`_m4sandbox.py` 验的是 core 的正确性，这个脚本验的是「双击能不能打开」——
`app/main.py` 里的 QApplication 构造、异常钩子、首次运行资源释放、真实
`%APPDATA%\\DocSorter` 配置路径，这几段在单元测试里都被绕过了。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT = Path(__file__).resolve().parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))


def main() -> int:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from app.config.settings import SettingsManager
    from app.main import configure_logging, install_exception_hook
    from app.ui.main_window import MainWindow

    configure_logging()
    install_exception_hook()

    app = QApplication(sys.argv)
    manager = SettingsManager()
    manager.ensure_user_files()

    window = MainWindow(manager)
    window.show()

    checks = {
        "整理流程页存在": window.sort_flow is not None,
        "历史记录页存在": window.history_page is not None,
        "结果页存在": window.sort_flow.result_page is not None,
        "执行服务已接线": window.sort_flow.execute_service is not None,
        "撤销服务已接线": window.sort_flow.undo_service is not None,
        "历史目录已建立": window.sort_flow.history.history_dir.is_dir(),
        "规则文件已释放": manager.rules_path.exists(),
        "窗口标题正确": window.windowTitle() == "DocSorter 文档分类工具",
    }

    QTimer.singleShot(300, window.close)
    QTimer.singleShot(600, app.quit)
    app.exec()

    failed = [label for label, ok in checks.items() if not ok]
    for label, ok in checks.items():
        print(f"  [{'OK  ' if ok else 'FAIL'}] {label}")
    print("\n启动自检通过。" if not failed else f"\n{len(failed)} 项未通过。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
