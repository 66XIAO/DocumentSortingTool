# Feature: document-sorting-tool, Property 40: 工程结构静态约束
"""属性 40。

- `app/core` 下全部模块的 import 不含 PySide6 / PyQt5 / PyQt6 / app.ui /
  app.services / app.config
- 整个代码库不 import PyQt5 或 PyQt6
- requirements.txt 每个依赖行以 == 精确锁定，且覆盖需求 18.5 列出的全部运行期依赖

这条属性不需要随机输入——它的「任意」是「代码库里的任意模块」，因此用穷举而非
Hypothesis：穷举比随机抽样更强。

Validates: Requirements 18.4, 18.5, 18.7
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP = PROJECT_ROOT / "app"

FORBIDDEN_IN_CORE = (
    "PySide6",
    "PySide2",
    "PyQt5",
    "PyQt6",
    "shiboken6",
    "shiboken2",
    "qfluentwidgets",
    "qframelesswindow",
    "app.ui",
    "app.services",
    "app.config",
)

FORBIDDEN_EVERYWHERE = ("PyQt5", "PyQt6", "PySide2")

#: 需求 18.5 点名的运行期依赖
REQUIRED_RUNTIME = (
    "PySide6",
    "PySide6-Fluent-Widgets",
    "PyYAML",
    "Send2Trash",
    "chardet",
    "keyring",
    "httpx",
    "pypdf",
    "python-docx",
    "openpyxl",
    "python-pptx",
)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            found.append(node.module)
    return found


def _matches(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == p or module.startswith(p + ".") for p in prefixes)


def test_core_layer_imports_nothing_forbidden() -> None:
    offenders = [
        f"{path.relative_to(PROJECT_ROOT).as_posix()}: {module}"
        for path in sorted((APP / "core").rglob("*.py"))
        for module in _imports(path)
        if _matches(module, FORBIDDEN_IN_CORE)
    ]

    assert not offenders, "core 层出现越界导入：\n" + "\n".join(offenders)


def test_codebase_never_imports_pyqt() -> None:
    offenders = [
        f"{path.relative_to(PROJECT_ROOT).as_posix()}: {module}"
        for path in sorted(PROJECT_ROOT.rglob("*.py"))
        if ".venv" not in path.parts and "site-packages" not in path.parts
        for module in _imports(path)
        if _matches(module, FORBIDDEN_EVERYWHERE)
    ]

    assert not offenders, "出现 PyQt / PySide2 导入：\n" + "\n".join(offenders)


def _requirement_lines() -> list[str]:
    text = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_every_requirement_is_pinned_exactly() -> None:
    unpinned = [
        line for line in _requirement_lines() if not re.match(r"^[A-Za-z0-9._\-\[\]]+==", line)
    ]

    assert not unpinned, "以下依赖未用 == 精确锁定：\n" + "\n".join(unpinned)


def test_required_runtime_dependencies_are_present() -> None:
    names = {line.split("==")[0].lower() for line in _requirement_lines()}

    missing = [dep for dep in REQUIRED_RUNTIME if dep.lower() not in names]

    assert not missing, f"requirements.txt 缺少运行期依赖: {missing}"


def test_qt_binding_is_lgpl_pyside6() -> None:
    """需求 18.4：必须是 PySide6（LGPL），不能是 PyQt6（GPL）。"""
    names = {line.split("==")[0].lower() for line in _requirement_lines()}

    assert "pyside6" in names
    assert "pyqt6" not in names
    assert "pyqt5" not in names


def test_only_one_fluent_widgets_variant_is_pinned() -> None:
    """四个 *-Fluent-Widgets 包顶层名同为 qfluentwidgets，共存会互相覆盖。"""
    names = {line.split("==")[0].lower() for line in _requirement_lines()}
    variants = {
        "pyqt-fluent-widgets",
        "pyqt6-fluent-widgets",
        "pyside2-fluent-widgets",
        "pyside6-fluent-widgets",
    }

    assert len(names & variants) == 1, f"存在多个组件库变体: {names & variants}"
