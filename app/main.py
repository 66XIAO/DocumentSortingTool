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
from pathlib import Path
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


def resource_dir() -> Path:
    """资源目录，兼容开发运行与 PyInstaller 产物两种布局。

    PyInstaller 把 spec 里 ``datas`` 声明的 ``resources`` 解包到 ``sys._MEIPASS``
    之下（onedir 模式即 ``_internal/resources``）；开发运行时它在仓库根。两处布局
    不同，但只有这一个函数需要知道这件事。
    """
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled) / "resources"
    return Path(__file__).resolve().parent.parent / "resources"


def app_icon_path() -> Path:
    return resource_dir() / "icon.ico"


def main() -> int:
    """应用主入口。"""
    configure_logging()
    install_exception_hook()

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationVersion(_app_version())

    # 窗口与任务栏图标。EXE 内嵌的那份只决定可执行文件本身的外观，运行起来之后
    # 窗口用的是这里设的图标；缺失时不阻断启动，只是没有图标
    icon_path = app_icon_path()
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    else:
        logger.warning("应用图标缺失: %s", icon_path)

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
