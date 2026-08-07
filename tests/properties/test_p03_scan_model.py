# Feature: document-sorting-tool, Property 3: 扫描输出与朴素模型一致
"""属性 3、4、5。

- 属性 3：扫描产出的 FileEntry 路径集合恰等于「扫描范围内各目录的直接子文件」，
  与子目录内容交集为空，不含 `.docsort`，include_hidden 为 false 时不含隐藏文件；
  子文件夹清单的三个统计值等于朴素递归遍历的结果。
- 属性 4：勾选/取消勾选构成往返；勾选新增的恰是直接子文件；下级不继承；已扫描过
  的目录不被重新读取；scope 与勾选集合是否为空严格对应。
- 属性 5：任意失败子集下，其余条目照常处理，失败条目 error 非空。

Validates: Requirements 2.2, 2.3, 2.4, 2.5, 2.7, 2.9, 2.10, 2.11, 2.16, 2.17, 2.19
"""

from __future__ import annotations

import os
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core import scanner as scanner_module
from app.core.models import ScanScope
from app.core.safety import SafetyGuard
from app.core.scanner import ScanSession
from tests.fixtures.generators import scan_options, tree_specs
from tests.fixtures.models import naive_recursive_stats, naive_scan
from tests.fixtures.trees import build_tree

FS_SETTINGS = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


def _guard() -> SafetyGuard:
    # tmp_path 位于 AppData 之下，必须关掉段黑名单才能作为根目录
    return SafetyGuard(system_roots=(), denied_segments=())


@FS_SETTINGS
@given(spec=tree_specs(), options=scan_options())
def test_entries_equal_naive_model(tmp_path_factory, spec, options) -> None:
    root = build_tree(tmp_path_factory.mktemp("scan"), spec)
    session = ScanSession(root, options=options, guard=_guard())

    result = session.initial_scan()

    expected = naive_scan(root, include_hidden=options.include_hidden)
    assert {e.path for e in result.entries} == expected


@FS_SETTINGS
@given(spec=tree_specs(), options=scan_options())
def test_no_entry_comes_from_a_subdirectory(tmp_path_factory, spec, options) -> None:
    """需求 2.3：子目录内容永不进 FileEntry 集合。"""
    root = build_tree(tmp_path_factory.mktemp("scan"), spec)
    session = ScanSession(root, options=options, guard=_guard())

    result = session.initial_scan()

    assert all(e.path.parent == root for e in result.entries)
    assert all(e.depth == 1 for e in result.entries)


@FS_SETTINGS
@given(spec=tree_specs(), options=scan_options())
def test_docsort_is_never_included(tmp_path_factory, spec, options) -> None:
    root = build_tree(tmp_path_factory.mktemp("scan"), spec)
    session = ScanSession(root, options=options, guard=_guard())

    result = session.initial_scan()

    assert not any(".docsort" in e.path.parts for e in result.entries)
    assert not any(s.name == ".docsort" for s in result.subfolders)


@FS_SETTINGS
@given(spec=tree_specs(), options=scan_options())
def test_subfolder_stats_equal_naive_model(tmp_path_factory, spec, options) -> None:
    """需求 2.4、2.5：三个统计值与朴素递归遍历一致。"""
    root = build_tree(tmp_path_factory.mktemp("scan"), spec)
    session = ScanSession(root, options=options, guard=_guard())

    result = session.initial_scan()

    for info in result.subfolders:
        loose, total, size, has_children = naive_recursive_stats(
            info.path, include_hidden=options.include_hidden
        )
        assert info.loose_file_count == loose, info.name
        assert info.recursive_file_count == total, info.name
        assert info.recursive_size == size, info.name
        assert info.has_children == has_children, info.name


@FS_SETTINGS
@given(spec=tree_specs())
def test_select_deselect_round_trip(tmp_path_factory, spec) -> None:
    """属性 4 的往返部分。"""
    root = build_tree(tmp_path_factory.mktemp("scan"), spec)
    session = ScanSession(root, guard=_guard())
    result = session.initial_scan()
    if not result.subfolders:
        return

    before_entries = {e.path for e in session.entries()}
    before_selection = set(session.selection().selected)

    for info in result.subfolders:
        session.select(info.path)
        session.deselect(info.path)

    assert {e.path for e in session.entries()} == before_entries
    assert session.selection().selected == before_selection


@FS_SETTINGS
@given(spec=tree_specs())
def test_select_adds_exactly_direct_children(tmp_path_factory, spec) -> None:
    root = build_tree(tmp_path_factory.mktemp("scan"), spec)
    session = ScanSession(root, guard=_guard())
    result = session.initial_scan()

    for info in result.subfolders:
        delta = session.select(info.path)
        expected = naive_scan(info.path) - naive_scan(root)
        assert {e.path for e in delta.added} == expected
        # 下级不继承勾选（需求 2.7）
        assert all(not child.selected for child in delta.subfolders)


@FS_SETTINGS
@given(spec=tree_specs())
def test_directories_are_never_read_twice(tmp_path_factory, spec) -> None:
    """属性 4：已扫描过的目录不被重新读取。"""
    root = build_tree(tmp_path_factory.mktemp("scan"), spec)
    session = ScanSession(root, guard=_guard())
    result = session.initial_scan()

    folders = [info.path for info in result.subfolders]
    for folder in folders:
        session.select(folder)
    for folder in folders:
        session.deselect(folder)
    for folder in folders:
        session.select(folder)

    for folder in folders:
        assert session.read_count(folder) == 1, folder
    assert session.read_count(root) == 1


@FS_SETTINGS
@given(spec=tree_specs())
def test_scope_tracks_selection_emptiness(tmp_path_factory, spec) -> None:
    """需求 2.9。"""
    root = build_tree(tmp_path_factory.mktemp("scan"), spec)
    session = ScanSession(root, guard=_guard())
    result = session.initial_scan()

    assert session.scope() is ScanScope.TOP_LEVEL_ONLY

    for info in result.subfolders:
        session.select(info.path)
        expected = (
            ScanScope.SELECTED_SUBFOLDERS
            if session.selection().selected
            else ScanScope.TOP_LEVEL_ONLY
        )
        assert session.scope() is expected


@FS_SETTINGS
@given(spec=tree_specs(), failing=st.sets(st.integers(0, 20), max_size=6))
def test_stat_failures_are_isolated(tmp_path_factory, spec, failing) -> None:
    """属性 5：任意失败子集下其余条目照常收集，失败条目 error 非空。"""
    root = build_tree(tmp_path_factory.mktemp("scan"), spec)
    names = sorted(p.name for p in root.iterdir() if p.is_file())
    doomed = {names[i % len(names)] for i in failing} if names else set()

    real = scanner_module._stat_of

    def flaky(entry: os.DirEntry[str]):
        if entry.name in doomed:
            return None
        return real(entry)

    scanner_module._stat_of = flaky
    try:
        session = ScanSession(root, guard=_guard())
        result = session.initial_scan()
    finally:
        scanner_module._stat_of = real

    by_name = {e.name: e for e in result.entries}
    assert set(by_name) == set(names)
    for name in names:
        if name in doomed:
            assert by_name[name].error, name
        else:
            assert by_name[name].error is None, name
    ok = sum(1 for e in result.entries if e.error is None)
    bad = sum(1 for e in result.entries if e.error)
    assert ok + bad == len(names)
