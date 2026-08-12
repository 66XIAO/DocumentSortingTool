# Feature: document-sorting-tool, Property 1: 根目录准入判定完备且确定
"""属性 1。

对任意路径输入，准入判定都返回 allow / reject 之一；reject 必附带封闭集合内的
原因码；盘符根判 drive_root；系统目录或 AppData 之内判 system_path；路径段仅
**包含** AppData 子串的不因此被拒。

Validates: Requirements 1.1, 1.2, 1.3
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.models import RejectReason, RootVerdict
from app.core.safety import SafetyGuard

_SEGMENTS = st.sampled_from(
    [
        "下载",
        "AppData",
        "appdata",
        "MyAppDataBackup",
        "AppDataX",
        "Windows",
        "WindowsBackup",
        "Program Files",
        "a b",
        "..",
        ".",
        "x",
    ]
)

paths = st.lists(_SEGMENTS, min_size=0, max_size=5).map(
    lambda parts: Path("C:/").joinpath(*parts)
)


@settings(max_examples=200)
@given(paths)
def test_verdict_is_total_and_reason_domain_is_closed(path: Path) -> None:
    guard = SafetyGuard()

    result = guard.check_root(path)

    assert result.verdict in (RootVerdict.ALLOW, RootVerdict.REJECT)
    if result.verdict is RootVerdict.REJECT:
        assert result.reason in set(RejectReason)
        assert result.message
    else:
        assert result.reason is None


@settings(max_examples=100)
@given(paths)
def test_verdict_is_deterministic(path: Path) -> None:
    guard = SafetyGuard()

    first = guard.check_root(path)
    second = guard.check_root(path)

    assert (first.verdict, first.reason) == (second.verdict, second.reason)


@settings(max_examples=50)
@given(st.sampled_from(["C:/", "D:/", "Z:/", "c:\\", "//server/share/"]))
def test_drive_root_is_rejected_as_drive_root(raw: str) -> None:
    """盘符根的各种等价写法都要判成 drive_root。"""
    guard = SafetyGuard()
    path = Path(raw)

    result = guard.check_root(path)

    # UNC 根可能要求凭据；Path.exists() 在 Windows 上不保证只返回 False，也可能抛
    # WinError 1326。准入函数必须自行容错，属性测试不应在独立的 exists() 上先崩。
    try:
        exists = path.exists()
    except OSError:
        exists = False

    # 不存在/不可访问的盘会先被 NOT_EXISTS 拦下，那也是合法判定；存在时必须是根目录
    assert result.reason in (RejectReason.DRIVE_ROOT, RejectReason.NOT_EXISTS)
    if exists:
        assert result.reason is RejectReason.DRIVE_ROOT


@settings(max_examples=200)
@given(st.lists(_SEGMENTS, min_size=1, max_size=4))
def test_appdata_segment_matching_is_exact_not_substring(parts: list[str]) -> None:
    """段相等才拒，仅包含子串的不拒——否则 D:\\MyAppDataBackup 会被误拒。"""
    guard = SafetyGuard(system_roots=())
    path = Path("D:/").joinpath(*parts)

    verdict = guard.is_system_path(path)

    has_exact_segment = any(p.lower() == "appdata" for p in path.parts)
    assert verdict is has_exact_segment


@settings(max_examples=100)
@given(st.lists(_SEGMENTS, min_size=0, max_size=3))
def test_within_system_root_is_system_path(parts: list[str]) -> None:
    system_root = Path("D:/FakeSystem")
    guard = SafetyGuard(system_roots=(system_root,), denied_segments=())

    inside = system_root.joinpath(*parts)
    sibling = Path("D:/FakeSystemBackup").joinpath(*parts)

    assert guard.is_system_path(inside) is True
    assert guard.is_system_path(sibling) is False
