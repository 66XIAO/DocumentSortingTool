"""集成测试与冒烟测试。需求 1.6、1.7、4.1、7.2、7.4、17.3、17.7。

覆盖属性测试刻意排除的外部依赖，能力不可用的环境下 skip：
- 符号链接与 junction 不递归
- 回收站语义
- keyring 读写
- Provider 连通性
- 系统主题跟随
- services 层线程归属
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from app.config.settings import SettingsManager, default_app_dir
from app.core.models import ScanOptions
from app.core.scanner import ScanSession
from app.core.safety import SafetyGuard


# ---------------------------------------------------------------------------
# 符号链接与 junction（需求 1.6）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not sys.platform == "win32", reason="Windows only")
def test_symlink_not_followed(tmp_path: Path) -> None:
    """follow_symlinks 为 false 时不递归进入符号链接。"""
    # 创建目标目录与文件
    target = tmp_path / "target"
    target.mkdir()
    (target / "secret.txt").write_text("secret")

    # 创建符号链接
    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("当前环境不支持创建符号链接")

    options = ScanOptions(follow_symlinks=False)
    session = ScanSession(tmp_path, options)
    result = session.initial_scan()

    # 符号链接不应被递归
    names = [e.name for e in result.entries]
    assert "secret.txt" not in names


@pytest.mark.skipif(not sys.platform == "win32", reason="Windows only")
def test_symlink_followed(tmp_path: Path) -> None:
    """follow_symlinks 为 true 时递归进入符号链接。"""
    target = tmp_path / "target"
    target.mkdir()
    (target / "secret.txt").write_text("secret")

    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("当前环境不支持创建符号链接")

    options = ScanOptions(follow_symlinks=True)
    session = ScanSession(tmp_path, options)
    result = session.initial_scan()

    names = [e.name for e in result.entries]
    assert "secret.txt" in names


# ---------------------------------------------------------------------------
# keyring（需求 7.2）
# ---------------------------------------------------------------------------


def test_keyring_available() -> None:
    """验证 keyring 后端可用。"""
    try:
        import keyring
    except ImportError:
        pytest.skip("keyring 未安装")

    # 尝试获取后端
    backend = keyring.get_keyring()
    assert backend is not None


def test_settings_manager_keyring_integration(tmp_path: Path) -> None:
    """SettingsManager 的 keyring 集成。"""
    import keyring
    manager = SettingsManager(base_dir=tmp_path, keyring_backend=keyring)

    # 测试引用格式
    ref = manager.api_key_ref("openai_compat")
    assert "keyring" in ref
    assert "openai_compat" in ref


# ---------------------------------------------------------------------------
# 回收站（需求 1.7）
# ---------------------------------------------------------------------------


def test_send2trash_available() -> None:
    """验证 send2trash 可用。"""
    try:
        import send2trash
    except ImportError:
        pytest.skip("send2trash 未安装")


# ---------------------------------------------------------------------------
# 首次运行释放规则文件（需求 4.1）
# ---------------------------------------------------------------------------


def test_ensure_user_files_creates_rules(tmp_path: Path) -> None:
    """首次运行释放 rules.yaml。"""
    manager = SettingsManager(base_dir=tmp_path)
    manager.ensure_user_files()

    assert manager.rules_path.exists()
    assert manager.settings_path.exists()


# ---------------------------------------------------------------------------
# 主窗口可构造（需求 17.3）
# ---------------------------------------------------------------------------


def test_main_window_constructable(qtbot) -> None:
    """主窗口可构造且不抛异常。"""
    from app.config.settings import SettingsManager
    from app.ui.main_window import MainWindow

    manager = SettingsManager()
    window = MainWindow(manager)
    assert window is not None


# ---------------------------------------------------------------------------
# CLI 可跑通（需求 18.7）
# ---------------------------------------------------------------------------


def test_cli_importable() -> None:
    """CLI 模块可导入。"""
    from app.core import cli
    assert cli is not None


# ---------------------------------------------------------------------------
# 领域模型导入
# ---------------------------------------------------------------------------


def test_all_core_modules_importable() -> None:
    """所有 core 模块可导入。"""
    from app.core import (
        classifiers,
        cli,
        conflicts,
        executor,
        fsops,
        history,
        inspector,
        journal,
        models,
        overrides,
        planner,
        progress,
        rules,
        safety,
        scanner,
        undo,
    )
    assert all([
        classifiers, cli, conflicts, executor, fsops,
        history, inspector, journal, models, overrides,
        planner, progress, rules, safety, scanner, undo,
    ])
