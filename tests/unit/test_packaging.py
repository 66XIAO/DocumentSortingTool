"""打包收尾项。需求 18.6、任务 59。

这些断言看着琐碎，但每一条都对应一个「构建能过、装完却不对」的真实故障：
    pyproject 的 readme 指向不存在的文件 -> 打 wheel 时报错
    spec 不引用图标 -> 产物是 PyInstaller 的默认图标
    图标只有 256 尺寸 -> 任务栏小图标由系统缩放而发虚
    资源没进 datas -> 开发时窗口有图标，装完没有
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SPEC = ROOT / "docsorter.spec"
ICON = ROOT / "resources" / "icon.ico"
README = ROOT / "README.md"
PYPROJECT = ROOT / "pyproject.toml"


def _spec_text() -> str:
    return SPEC.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# README 与 pyproject
# ---------------------------------------------------------------------------


def test_readme_exists() -> None:
    assert README.is_file(), "pyproject 的 readme 指向它，缺失会让打包直接失败"


def test_pyproject_readme_points_at_existing_file() -> None:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    readme = data["project"]["readme"]

    assert (ROOT / readme).is_file(), f"readme 指向的文件不存在: {readme}"


def test_readme_documents_run_and_build_commands() -> None:
    """自用工具的 README 至少要能回答「怎么跑」「怎么打包」。"""
    text = README.read_text(encoding="utf-8")

    assert "python -m app.main" in text
    assert "docsorter.spec" in text
    assert "requirements.txt" in text


def test_readme_states_self_use_only_licensing() -> None:
    """任务 58 定档为仅自用不分发，README 必须写清这条边界。"""
    text = README.read_text(encoding="utf-8")

    assert "仅自用" in text
    assert "GPLv3" in text


def test_no_license_file_is_present() -> None:
    """定档为不分发，因此刻意不放 LICENSE——放了等于声明对外授权口径。"""
    candidates = [ROOT / name for name in ("LICENSE", "LICENSE.txt", "LICENSE.md")]

    assert not [p for p in candidates if p.exists()]


# ---------------------------------------------------------------------------
# 图标
# ---------------------------------------------------------------------------


def test_icon_exists() -> None:
    assert ICON.is_file(), "运行 python tools/make_icon.py 生成"


def test_icon_has_multiple_sizes() -> None:
    """Windows 在任务栏、Alt-Tab、资源管理器各处取不同尺寸。"""
    pytest.importorskip("PIL", reason="需要 Pillow 才能读 ico 的尺寸表")
    from PIL import Image

    with Image.open(ICON) as image:
        sizes = {size[0] for size in image.ico.sizes()}

    assert {16, 32, 48, 256} <= sizes, f"尺寸不全: {sorted(sizes)}"


def test_icon_generator_is_committed() -> None:
    """图标是二进制产物，生成脚本必须在仓库里，否则没人知道怎么改。"""
    assert (ROOT / "tools" / "make_icon.py").is_file()


# ---------------------------------------------------------------------------
# spec
# ---------------------------------------------------------------------------


def test_spec_references_icon() -> None:
    """需求 18.6：产物不该带 PyInstaller 的默认图标。"""
    text = _spec_text()
    match = re.search(r"^\s*icon\s*=\s*'([^']+)'", text, re.MULTILINE)

    assert match is not None, "spec 未设置 icon"
    assert (ROOT / match.group(1)).is_file()


def test_spec_bundles_resources_for_runtime() -> None:
    """EXE 内嵌的图标只决定可执行文件外观，窗口图标要读运行期文件。"""
    text = _spec_text()

    assert "'resources/icon.ico', 'resources'" in text


def test_spec_bundles_default_rules() -> None:
    """需求 18.6：rules_default.yaml 必须在包内，首次运行要释放它。"""
    assert "'app/config/rules_default.yaml', 'app/config'" in _spec_text()


def test_spec_is_windowed() -> None:
    """需求 18.6：桌面工具不该弹控制台。"""
    assert "console=False" in _spec_text()


# ---------------------------------------------------------------------------
# 运行期资源定位
# ---------------------------------------------------------------------------


def test_resource_dir_resolves_in_dev_layout() -> None:
    from app.main import app_icon_path, resource_dir

    assert resource_dir() == ROOT / "resources"
    assert app_icon_path().is_file()


def test_resource_dir_follows_meipass_when_frozen(monkeypatch) -> None:
    """PyInstaller 产物里资源在 sys._MEIPASS 之下，布局与开发时不同。"""
    import sys

    from app.main import resource_dir

    monkeypatch.setattr(sys, "_MEIPASS", r"C:\frozen\_internal", raising=False)

    assert resource_dir() == Path(r"C:\frozen\_internal") / "resources"
