"""SafetyGuard 的例子级与边界测试。

正式的属性 1（准入判定完备且确定）与属性 2（目标路径永不逃逸）在任务 12、21 中
实现。本文件钉住几个容易写错、且写错后后果严重的点：

    AppData 必须按路径段匹配，否则 D:\\MyAppDataBackup 会被误拒
    check_target 必须在 resolve() 之后比较，否则 root\\..\\..\\X 能绕过
    junction 与符号链接都要拦，只判 is_symlink 会漏掉 junction
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.models import RejectReason, RootVerdict
from app.core.safety import AdmissionResult, SafetyGuard, describe_reason


@pytest.fixture
def guard(tmp_path: Path) -> SafetyGuard:
    """注入临时目录作为「系统目录」，避免测试依赖真实的 C:\\Windows。

    ``denied_segments=()`` 是必须的：Windows 上 pytest 的 tmp_path 位于
    ``C:\\Users\\<用户>\\AppData\\Local\\Temp`` 之下，默认黑名单会把每个临时目录
    都判成系统路径，测试就没有「可用根目录」这个立足点了。段匹配语义由
    ``segment_guard`` 单独验证。
    """
    fake_system = tmp_path / "FakeWindows"
    fake_system.mkdir()
    return SafetyGuard(system_roots=(fake_system,), denied_segments=())


@pytest.fixture
def segment_guard(tmp_path: Path) -> SafetyGuard:
    """用一个不会出现在临时路径里的段来验证段匹配语义。

    直接用 "appdata" 验证是无效的——tmp_path 本身就含这一段，无论段匹配写成
    相等还是包含，结果都是拒绝，测不出区别。
    """
    return SafetyGuard(system_roots=(), denied_segments=("secretzone",))


# ---------------------------------------------------------------------------
# AdmissionResult 的构造期不变式
# ---------------------------------------------------------------------------


def test_reject_requires_reason() -> None:
    with pytest.raises(ValueError):
        AdmissionResult(verdict=RootVerdict.REJECT, reason=None)


def test_allow_must_not_carry_reason() -> None:
    with pytest.raises(ValueError):
        AdmissionResult(verdict=RootVerdict.ALLOW, reason=RejectReason.DRIVE_ROOT)


def test_every_reason_has_a_chinese_message() -> None:
    for reason in RejectReason:
        message = describe_reason(reason)
        assert message
        assert message != "该目录不可用。"


# ---------------------------------------------------------------------------
# check_root
# ---------------------------------------------------------------------------


def test_normal_directory_is_allowed(guard: SafetyGuard, tmp_path: Path) -> None:
    target = tmp_path / "下载"
    target.mkdir()

    result = guard.check_root(target)

    assert result.allowed
    assert result.verdict is RootVerdict.ALLOW
    assert result.reason is None
    assert result.resolved == target.resolve()


def test_missing_directory_is_rejected(guard: SafetyGuard, tmp_path: Path) -> None:
    result = guard.check_root(tmp_path / "不存在")

    assert not result.allowed
    assert result.reason is RejectReason.NOT_EXISTS


def test_file_is_rejected_as_not_a_dir(guard: SafetyGuard, tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")

    result = guard.check_root(f)

    assert result.reason is RejectReason.NOT_A_DIR


def test_drive_root_is_rejected(guard: SafetyGuard) -> None:
    anchor = Path(Path.cwd().anchor)

    result = guard.check_root(anchor)

    assert not result.allowed
    assert result.reason is RejectReason.DRIVE_ROOT


def test_system_directory_itself_is_rejected(guard: SafetyGuard) -> None:
    (system_root,) = guard.system_roots

    result = guard.check_root(system_root)

    assert result.reason is RejectReason.SYSTEM_PATH


def test_inside_system_directory_is_rejected(guard: SafetyGuard) -> None:
    (system_root,) = guard.system_roots
    nested = system_root / "System32" / "drivers"
    nested.mkdir(parents=True)

    result = guard.check_root(nested)

    assert result.reason is RejectReason.SYSTEM_PATH


def test_system_directory_match_is_case_insensitive(guard: SafetyGuard) -> None:
    (system_root,) = guard.system_roots
    weird_case = Path(str(system_root).upper())

    assert guard.is_system_path(weird_case)


def test_appdata_is_in_default_denied_segments() -> None:
    """需求 1.3 要求拒绝任一 AppData 之内的路径。"""
    assert "appdata" in SafetyGuard().denied_segments


def test_appdata_path_is_system_path_without_touching_disk() -> None:
    """is_system_path 是纯判定，不需要路径真实存在。"""
    guard = SafetyGuard(system_roots=())

    assert guard.is_system_path(Path(r"C:\Users\someone\AppData\Roaming\App"))
    assert guard.is_system_path(Path(r"C:\Users\someone\appdata\Local"))
    assert not guard.is_system_path(Path(r"D:\MyAppDataBackup\2024"))


def test_denied_segment_is_rejected(segment_guard: SafetyGuard, tmp_path: Path) -> None:
    nested = tmp_path / "SecretZone" / "inner"
    nested.mkdir(parents=True)

    result = segment_guard.check_root(nested)

    assert result.reason is RejectReason.SYSTEM_PATH


def test_denied_segment_lookalike_is_not_rejected(
    segment_guard: SafetyGuard, tmp_path: Path
) -> None:
    """按段匹配而非字符串包含：MySecretZoneBackup 不该被拒。"""
    lookalike = tmp_path / "MySecretZoneBackup"
    lookalike.mkdir()

    result = segment_guard.check_root(lookalike)

    assert result.allowed


def test_denied_segment_match_ignores_case(
    segment_guard: SafetyGuard, tmp_path: Path
) -> None:
    nested = tmp_path / "SECRETZONE"
    nested.mkdir()

    assert segment_guard.check_root(nested).reason is RejectReason.SYSTEM_PATH


def test_sibling_of_system_root_is_not_rejected(
    guard: SafetyGuard, tmp_path: Path
) -> None:
    """前缀相同但不是子目录：FakeWindowsBackup 不该被 FakeWindows 命中。"""
    sibling = tmp_path / "FakeWindowsBackup"
    sibling.mkdir()

    result = guard.check_root(sibling)

    assert result.allowed


def test_verdict_is_always_one_of_two_values(guard: SafetyGuard, tmp_path: Path) -> None:
    ok = tmp_path / "ok"
    ok.mkdir()
    candidates = [
        ok,
        tmp_path / "missing",
        Path(Path.cwd().anchor),
        guard.system_roots[0],
    ]

    for candidate in candidates:
        result = guard.check_root(candidate)
        assert result.verdict in (RootVerdict.ALLOW, RootVerdict.REJECT)
        if result.verdict is RootVerdict.REJECT:
            assert result.reason in set(RejectReason)


# ---------------------------------------------------------------------------
# check_target
# ---------------------------------------------------------------------------


def test_target_inside_root_passes(guard: SafetyGuard, tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    assert guard.check_target(root, root / "文档" / "PDF" / "a.pdf")


def test_root_itself_counts_as_inside(guard: SafetyGuard, tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    assert guard.check_target(root, root)


def test_dotdot_escape_is_rejected(guard: SafetyGuard, tmp_path: Path) -> None:
    """必须在 resolve() 之后比较，否则这条会通过。"""
    root = tmp_path / "root"
    root.mkdir()
    escaping = root / ".." / ".." / "外面.pdf"

    assert not guard.check_target(root, escaping)


def test_sibling_directory_is_rejected(guard: SafetyGuard, tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    assert not guard.check_target(root, tmp_path / "rootside" / "a.pdf")


def test_target_check_is_case_insensitive(guard: SafetyGuard, tmp_path: Path) -> None:
    root = tmp_path / "Root"
    root.mkdir()
    target = Path(str(root).lower()) / "a.pdf"

    assert guard.check_target(root, target)


# ---------------------------------------------------------------------------
# is_traversable
# ---------------------------------------------------------------------------


def _entry_for(directory: Path, name: str) -> os.DirEntry[str]:
    with os.scandir(directory) as it:
        for entry in it:
            if entry.name == name:
                return entry
    raise AssertionError(f"{name} 不在 {directory} 中")


def test_plain_directory_is_traversable(guard: SafetyGuard, tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()

    entry = _entry_for(tmp_path, "sub")

    assert guard.is_traversable(entry, follow_symlinks=False)


def test_file_is_not_traversable(guard: SafetyGuard, tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")

    entry = _entry_for(tmp_path, "a.txt")

    assert not guard.is_traversable(entry, follow_symlinks=False)


def test_symlinked_directory_is_skipped_when_not_following(
    guard: SafetyGuard, tmp_path: Path
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接（Windows 需要开发者模式或管理员）")

    entry = _entry_for(tmp_path, "link")

    assert not guard.is_traversable(entry, follow_symlinks=False)
    assert guard.is_traversable(entry, follow_symlinks=True)
