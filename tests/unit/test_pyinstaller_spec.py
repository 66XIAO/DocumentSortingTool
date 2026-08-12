"""PyInstaller spec 断言测试。需求 18.6。

验证 spec 的 datas 项包含 rules_default.yaml。
"""

from __future__ import annotations

from pathlib import Path


def test_spec_file_exists() -> None:
    """spec 文件应存在。"""
    spec_path = Path(__file__).resolve().parent.parent.parent / "docsorter.spec"
    assert spec_path.exists(), f"spec 文件不存在: {spec_path}"


def test_spec_includes_rules_default_yaml() -> None:
    """需求 18.6：spec 的 datas 项包含 rules_default.yaml。"""
    spec_path = Path(__file__).resolve().parent.parent.parent / "docsorter.spec"
    content = spec_path.read_text(encoding="utf-8")

    # datas 列表应包含 rules_default.yaml
    assert "rules_default.yaml" in content, "spec 中未包含 rules_default.yaml"


def test_spec_has_noconsole() -> None:
    """需求 18.6：--noconsole（console=False）。"""
    spec_path = Path(__file__).resolve().parent.parent.parent / "docsorter.spec"
    content = spec_path.read_text(encoding="utf-8")

    assert "console=False" in content, "spec 中未设置 console=False"


def test_spec_excludes_unused_qt_modules() -> None:
    """需求 18.6：裁剪未使用的 Qt 模块。"""
    spec_path = Path(__file__).resolve().parent.parent.parent / "docsorter.spec"
    content = spec_path.read_text(encoding="utf-8")

    # 应排除明显的非必要模块
    excluded_modules = ["QtWebEngine", "QtMultimedia", "QtCharts"]
    for module in excluded_modules:
        assert module in content, f"spec 中未排除 {module}"


def test_rules_default_yaml_exists() -> None:
    """验证 rules_default.yaml 源文件存在。"""
    yaml_path = Path(__file__).resolve().parent.parent.parent / "app" / "config" / "rules_default.yaml"
    assert yaml_path.exists(), f"rules_default.yaml 不存在: {yaml_path}"
