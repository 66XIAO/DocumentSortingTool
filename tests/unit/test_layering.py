"""分层约束的静态守卫。

对应需求 18.7 与 design.md「分层与依赖方向」。这两条约束是整份设计的地基：
core 层零 Qt 依赖，才能让 42 条正确性属性用 pytest + Hypothesis 直接跑而不必
拉起 QApplication；qfluentwidgets 的引用收敛在 theme 层，才能让组件库授权
（GPLv3）带来的更换风险改动面可控。

约束靠人自觉守不住，所以用 AST 扫描静态验证。
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP = PROJECT_ROOT / "app"

QT_PREFIXES = ("PySide6", "PySide2", "PyQt5", "PyQt6", "shiboken6", "shiboken2")
WIDGET_LIB_PREFIXES = ("qfluentwidgets", "qframelesswindow")

# core 不得看见 Qt，也不得看见上层与配置层。
# 配置以窄选项对象注入，保证 config -> core 单向不成环。
CORE_FORBIDDEN = (
    *QT_PREFIXES,
    *WIDGET_LIB_PREFIXES,
    "app.services",
    "app.ui",
    "app.config",
)


def _iter_python_files(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    for path in sorted(root.rglob("*.py")):
        yield path


def _imported_modules(path: Path) -> Iterator[str]:
    """产出该文件中所有绝对导入的模块名。

    相对导入（`from . import x`）不可能跨层，直接跳过。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            if node.module:
                yield node.module


def _matches(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == p or module.startswith(p + ".") for p in prefixes)


def _violations(root: Path, forbidden: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for path in _iter_python_files(root):
        for module in _imported_modules(path):
            if _matches(module, forbidden):
                rel = path.relative_to(PROJECT_ROOT).as_posix()
                found.append(f"{rel}: import {module}")
    return found


def test_core_layer_stays_free_of_qt_and_upper_layers() -> None:
    """core 层不得 import Qt、组件库、services、ui、config。"""
    offenders = _violations(APP / "core", CORE_FORBIDDEN)
    assert not offenders, "core 层出现越界导入：\n" + "\n".join(offenders)


def test_widget_library_imports_are_confined_to_theme() -> None:
    """qfluentwidgets 只允许出现在 app/ui/theme 内。"""
    offenders: list[str] = []
    for subpackage in ("pages", "widgets"):
        offenders += _violations(APP / "ui" / subpackage, WIDGET_LIB_PREFIXES)

    # 再查 app/ui 直属文件（如 main_window.py），theme 子包除外
    for path in sorted((APP / "ui").glob("*.py")):
        for module in _imported_modules(path):
            if _matches(module, WIDGET_LIB_PREFIXES):
                rel = path.relative_to(PROJECT_ROOT).as_posix()
                offenders.append(f"{rel}: import {module}")

    assert not offenders, (
        "组件库引用泄漏到 theme 层之外：\n"
        + "\n".join(offenders)
        + "\n请改为从 app.ui.theme 取组件。"
    )


def test_guarded_directories_exist() -> None:
    """守卫本身要有意义：被扫描的目录必须真实存在。

    否则包结构一旦被挪动，上面两条测试会静默变成空断言。
    """
    for rel in ("core", "services", "ui", "ui/pages", "ui/widgets", "ui/theme", "config"):
        assert (APP / rel).is_dir(), f"缺少目录 app/{rel}"
