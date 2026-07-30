"""DocSorter 应用入口。

职责限于装配：QApplication 构造、全局异常钩子安装、首次运行资源释放的调用点、
主窗口拉起。业务逻辑一律不在此处。

注意 MainWindow 与 SettingsManager 都在函数体内延迟 import，使 `import app.main`
本身不触发 Qt 加载，也不依赖尚未实现的模块。
"""

from __future__ import annotations

import logging
import sys
import traceback
from types import TracebackType

logger = logging.getLogger("docsorter")

APP_NAME = "DocSorter"
ORG_NAME = "DocSorter"


def install_exception_hook() -> None:
    """安装全局异常钩子。

    未捕获异常必须留下可追溯记录而不是静默吞掉——这是本工具会改动用户文件的
    直接后果。钩子只负责记录并提示，不尝试恢复。
    """

    def _hook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_tb: TracebackType | None,
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return

        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logger.critical("未捕获异常:\n%s", detail)

        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                QMessageBox.critical(
                    None,
                    f"{APP_NAME} 发生未处理的错误",
                    f"{exc_type.__name__}: {exc_value}\n\n详细信息已写入日志。",
                )
        except Exception:  # noqa: BLE001 - 钩子内部绝不能再抛
            pass

    sys.excepthook = _hook


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


def main() -> int:
    """应用主入口。"""
    configure_logging()
    install_exception_hook()

    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationVersion(_app_version())

    # 首次运行把随包的 rules_default.yaml 释放到 %APPDATA%\DocSorter\rules.yaml
    # 具体实现见任务 7 的 SettingsManager。
    from app.config.settings import SettingsManager

    settings_manager = SettingsManager()
    settings_manager.ensure_user_files()

    from app.ui.main_window import MainWindow

    window = MainWindow(settings_manager)
    window.show()
    return app.exec()


def _app_version() -> str:
    from app import __version__

    return __version__


if __name__ == "__main__":
    raise SystemExit(main())
