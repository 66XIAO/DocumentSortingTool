"""Scanner 与 ScanSession 的例子级与边界测试。

属性 3/4/5 的 Hypothesis 版本在任务 12。本文件钉住扫描范围语义——这是「目录结构
默认不被改动」这条红线的实现落点，写错的后果是用户已经整理好的文件夹被打散。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core import scanner as scanner_module
from app.core.models import ScanOptions, ScanScope
from app.core.progress import CancelToken, ProgressSnapshot
from app.core.safety import SafetyGuard
from app.core.scanner import ScanSession
from tests.fixtures.models import naive_recursive_stats, naive_scan
from tests.fixtures.trees import build_tree, set_hidden

TREE = {
    "报告.pdf": "顶层孤立文件",
    "发票_2024.pdf": "x",
    "README": "无扩展名",
    "旧资料": {
        "合同.docx": "合同内容",
        "备忘.txt": "备忘",
        "更深": {
            "深层.txt": "深层内容",
            "再深": {"底.txt": "底"},
        },
    },
    "微信文件": {"图片.png": "png"},
    "空文件夹": {},
    ".docsort": {"journal.jsonl": "{}", "history": {"a.json": "{}"}},
}


@pytest.fixture
def guard(tmp_path: Path) -> SafetyGuard:
    """tmp_path 在 Windows 上位于 AppData 之下，必须关掉段黑名单才能作为根目录。"""
    return SafetyGuard(system_roots=(), denied_segments=())


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return build_tree(tmp_path / "下载", TREE)


def _session(root: Path, guard: SafetyGuard, **kw: object) -> ScanSession:
    options = kw.pop("options", ScanOptions())
    return ScanSession(root, options=options, guard=guard, **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# initial_scan：只收孤立文件
# ---------------------------------------------------------------------------


def test_initial_scan_collects_only_root_loose_files(root: Path, guard: SafetyGuard) -> None:
    session = _session(root, guard)

    result = session.initial_scan()

    assert {e.name for e in result.entries} == {"报告.pdf", "发票_2024.pdf", "README"}


def test_subdirectory_contents_never_enter_entries(root: Path, guard: SafetyGuard) -> None:
    """需求 2.3：子文件夹自身及其内部内容都不进 FileEntry 集合。"""
    session = _session(root, guard)

    result = session.initial_scan()
    paths = {e.path for e in result.entries}

    assert root / "旧资料" / "合同.docx" not in paths
    assert root / "旧资料" / "更深" / "深层.txt" not in paths
    assert not any(p.parent != root for p in paths)


def test_initial_scan_matches_naive_model(root: Path, guard: SafetyGuard) -> None:
    session = _session(root, guard)

    result = session.initial_scan()

    assert {e.path for e in result.entries} == naive_scan(root)


def test_entries_order_is_stable(root: Path, guard: SafetyGuard) -> None:
    a = _session(root, guard).initial_scan().entries
    b = _session(root, guard).initial_scan().entries

    assert [e.path for e in a] == [e.path for e in b]


def test_ext_is_lowercase_with_leading_dot(tmp_path: Path, guard: SafetyGuard) -> None:
    r = build_tree(tmp_path / "r", {"A.PDF": "x", "b.TaR.Gz": "y", "README": "z"})
    session = _session(r, guard)

    by_name = {e.name: e for e in session.initial_scan().entries}

    assert by_name["A.PDF"].ext == ".pdf"
    assert by_name["b.TaR.Gz"].ext == ".gz"
    assert by_name["README"].ext == ""


def test_depth_is_relative_to_root(root: Path, guard: SafetyGuard) -> None:
    session = _session(root, guard)
    session.initial_scan()

    assert all(e.depth == 1 for e in session.entries())

    session.select(root / "旧资料")
    deeper = [e for e in session.entries() if e.name == "合同.docx"]

    assert deeper and deeper[0].depth == 2


def test_elapsed_ms_uses_injected_clock(root: Path, guard: SafetyGuard) -> None:
    ticks = iter([10.0, 10.25])
    session = _session(root, guard, clock=lambda: next(ticks))

    assert session.initial_scan().elapsed_ms == 250


# ---------------------------------------------------------------------------
# 子文件夹清单
# ---------------------------------------------------------------------------


def test_subfolder_list_covers_direct_children_only(root: Path, guard: SafetyGuard) -> None:
    session = _session(root, guard)

    result = session.initial_scan()

    assert {s.name for s in result.subfolders} == {"旧资料", "微信文件", "空文件夹"}


def test_subfolder_stats_match_naive_model(root: Path, guard: SafetyGuard) -> None:
    session = _session(root, guard)

    result = session.initial_scan()
    by_name = {s.name: s for s in result.subfolders}

    for name in ("旧资料", "微信文件", "空文件夹"):
        loose, total, size, has_children = naive_recursive_stats(root / name)
        info = by_name[name]
        assert info.loose_file_count == loose, name
        assert info.recursive_file_count == total, name
        assert info.recursive_size == size, name
        assert info.has_children == has_children, name


def test_loose_and_recursive_counts_differ_for_nested_tree(
    root: Path, guard: SafetyGuard
) -> None:
    """区分这两个数字是子文件夹选择器的信息价值所在。"""
    session = _session(root, guard)
    by_name = {s.name: s for s in session.initial_scan().subfolders}

    old = by_name["旧资料"]
    assert old.loose_file_count == 2
    assert old.recursive_file_count == 4
    assert old.has_children is True


def test_empty_subfolder_has_no_children(root: Path, guard: SafetyGuard) -> None:
    session = _session(root, guard)
    by_name = {s.name: s for s in session.initial_scan().subfolders}

    empty = by_name["空文件夹"]
    assert empty.recursive_file_count == 0
    assert empty.has_children is False


def test_subfolder_stats_do_not_leak_into_entries(root: Path, guard: SafetyGuard) -> None:
    """需求 2.5：统计只累加数字，被统计到的文件不进 entries。"""
    session = _session(root, guard)

    result = session.initial_scan()

    assert len(result.entries) == 3
    assert sum(s.recursive_file_count for s in result.subfolders) == 5


# ---------------------------------------------------------------------------
# 排除规则
# ---------------------------------------------------------------------------


def test_docsort_is_excluded_from_entries_and_subfolders(
    root: Path, guard: SafetyGuard
) -> None:
    session = _session(root, guard)

    result = session.initial_scan()

    assert ".docsort" not in {s.name for s in result.subfolders}
    assert not any(".docsort" in e.path.parts for e in result.entries)


def test_docsort_is_excluded_at_any_depth(tmp_path: Path, guard: SafetyGuard) -> None:
    """需求 2.19 只写了根目录，但那是我们自己的元数据目录，任意层级都不该被整理。"""
    r = build_tree(
        tmp_path / "r",
        {"a.txt": "x", "子目录": {".docsort": {"j.jsonl": "{}"}, "b.txt": "y"}},
    )
    session = _session(r, guard)

    result = session.initial_scan()
    by_name = {s.name: s for s in result.subfolders}

    assert by_name["子目录"].recursive_file_count == 1
    assert by_name["子目录"].has_children is False


def test_custom_excluded_names_are_honoured(tmp_path: Path, guard: SafetyGuard) -> None:
    r = build_tree(tmp_path / "r", {"keep.txt": "x", "node_modules": {"junk.js": "y"}})
    session = _session(
        r, guard, options=ScanOptions(excluded_names=frozenset({"node_modules"}))
    )

    result = session.initial_scan()

    assert {s.name for s in result.subfolders} == set()
    assert {e.name for e in result.entries} == {"keep.txt"}


def test_excluded_name_match_ignores_case(tmp_path: Path, guard: SafetyGuard) -> None:
    r = build_tree(tmp_path / "r", {".DOCSORT": {"j.jsonl": "{}"}, "a.txt": "x"})
    session = _session(r, guard)

    assert {s.name for s in session.initial_scan().subfolders} == set()


# ---------------------------------------------------------------------------
# 隐藏文件
# ---------------------------------------------------------------------------


def test_dotfiles_are_hidden_by_default(tmp_path: Path, guard: SafetyGuard) -> None:
    r = build_tree(tmp_path / "r", {".env": "SECRET=1", "a.txt": "x"})
    session = _session(r, guard)

    assert {e.name for e in session.initial_scan().entries} == {"a.txt"}


def test_include_hidden_brings_dotfiles_back(tmp_path: Path, guard: SafetyGuard) -> None:
    r = build_tree(tmp_path / "r", {".env": "SECRET=1", "a.txt": "x"})
    session = _session(r, guard, options=ScanOptions(include_hidden=True))

    entries = session.initial_scan().entries

    assert {e.name for e in entries} == {".env", "a.txt"}
    assert next(e for e in entries if e.name == ".env").is_hidden is True


def test_windows_hidden_attribute_is_respected(tmp_path: Path, guard: SafetyGuard) -> None:
    r = build_tree(tmp_path / "r", {"普通.txt": "x", "隐藏.txt": "y"})
    if not set_hidden(r / "隐藏.txt"):
        pytest.skip("当前环境无法设置 Windows 隐藏属性")

    session = _session(r, guard)

    assert {e.name for e in session.initial_scan().entries} == {"普通.txt"}


def test_hidden_directories_are_excluded_from_subfolder_list(
    tmp_path: Path, guard: SafetyGuard
) -> None:
    r = build_tree(tmp_path / "r", {"a.txt": "x", ".git": {"config": "y"}})
    session = _session(r, guard)

    assert {s.name for s in session.initial_scan().subfolders} == set()


# ---------------------------------------------------------------------------
# select / deselect
# ---------------------------------------------------------------------------


def test_select_adds_only_direct_children_of_folder(root: Path, guard: SafetyGuard) -> None:
    session = _session(root, guard)
    session.initial_scan()

    delta = session.select(root / "旧资料")

    assert {e.name for e in delta.added} == {"合同.docx", "备忘.txt"}
    assert not any(e.name == "深层.txt" for e in delta.added)


def test_select_does_not_cascade_to_children(root: Path, guard: SafetyGuard) -> None:
    """需求 2.7：下级子文件夹保持未勾选，直到用户单独勾选。"""
    session = _session(root, guard)
    session.initial_scan()

    delta = session.select(root / "旧资料")

    assert session.selection().selected == {root / "旧资料"}
    assert all(not s.selected for s in delta.subfolders)
    assert {s.name for s in delta.subfolders} == {"更深"}


def test_select_updates_scope(root: Path, guard: SafetyGuard) -> None:
    session = _session(root, guard)
    session.initial_scan()
    assert session.scope() is ScanScope.TOP_LEVEL_ONLY

    session.select(root / "旧资料")
    assert session.scope() is ScanScope.SELECTED_SUBFOLDERS

    session.deselect(root / "旧资料")
    assert session.scope() is ScanScope.TOP_LEVEL_ONLY


def test_select_then_deselect_round_trips(root: Path, guard: SafetyGuard) -> None:
    """属性 4 的往返部分。"""
    session = _session(root, guard)
    session.initial_scan()
    before_entries = {e.path for e in session.entries()}
    before_selection = set(session.selection().selected)

    session.select(root / "旧资料")
    session.deselect(root / "旧资料")

    assert {e.path for e in session.entries()} == before_entries
    assert session.selection().selected == before_selection


def test_deselect_reports_removed_paths(root: Path, guard: SafetyGuard) -> None:
    session = _session(root, guard)
    session.initial_scan()
    session.select(root / "旧资料")

    delta = session.deselect(root / "旧资料")

    assert {p.name for p in delta.removed} == {"合同.docx", "备忘.txt"}


def test_reselect_does_not_reread_directory(root: Path, guard: SafetyGuard) -> None:
    """属性 4：已扫描过的目录不被重新读取。"""
    session = _session(root, guard)
    session.initial_scan()
    folder = root / "旧资料"

    session.select(folder)
    reads_after_first = session.read_count(folder)
    session.deselect(folder)
    session.select(folder)

    assert reads_after_first == 1
    assert session.read_count(folder) == 1


def test_selecting_child_without_parent_is_allowed(root: Path, guard: SafetyGuard) -> None:
    """需求 2.7 允许「只勾子不勾父」，design.md 的生成器维度表也把它列为必测组合。"""
    session = _session(root, guard)
    session.initial_scan()
    child = root / "旧资料" / "更深"

    delta = session.select(child)

    assert session.selection().selected == {child}
    assert {e.name for e in delta.added} == {"深层.txt"}
    assert not any(e.name == "合同.docx" for e in session.entries())


def test_deselecting_parent_leaves_child_selected(root: Path, guard: SafetyGuard) -> None:
    """deselect 必须是 select 的精确逆操作。

    design.md 写的是「取消勾选 S 时把 S 的下级也从 selection 移除」，但那样
    deselect 就不是 select 的逆操作，属性 4 的往返在「下级原本已勾选」的场景下
    必然失败；需求 2.11 也只要求移除 S 自己的孤立文件。故以需求与属性为准。
    """
    session = _session(root, guard)
    session.initial_scan()
    parent = root / "旧资料"
    child = parent / "更深"
    session.select(parent)
    session.select(child)

    session.deselect(parent)

    assert session.selection().selected == {child}
    assert any(e.name == "深层.txt" for e in session.entries())
    assert not any(e.name == "合同.docx" for e in session.entries())


def test_double_select_is_idempotent(root: Path, guard: SafetyGuard) -> None:
    session = _session(root, guard)
    session.initial_scan()
    folder = root / "旧资料"

    session.select(folder)
    first = {e.path for e in session.entries()}
    delta = session.select(folder)

    assert delta.added == []
    assert {e.path for e in session.entries()} == first


def test_deselect_unselected_folder_is_noop(root: Path, guard: SafetyGuard) -> None:
    session = _session(root, guard)
    session.initial_scan()

    delta = session.deselect(root / "微信文件")

    assert delta.removed == []
    assert delta.added == []


def test_selecting_root_itself_is_rejected(root: Path, guard: SafetyGuard) -> None:
    session = _session(root, guard)
    session.initial_scan()

    delta = session.select(root)

    assert delta.added == []
    assert session.selection().selected == set()


def test_selecting_outside_root_is_rejected(
    root: Path, tmp_path: Path, guard: SafetyGuard
) -> None:
    outside = build_tree(tmp_path / "外面", {"a.txt": "x"})
    session = _session(root, guard)
    session.initial_scan()

    delta = session.select(outside)

    assert delta.added == []
    assert session.selection().selected == set()


def test_set_selected_dispatches_both_ways(root: Path, guard: SafetyGuard) -> None:
    session = _session(root, guard)
    session.initial_scan()
    folder = root / "微信文件"

    session.set_selected(folder, True)
    assert folder in session.selection().selected

    session.set_selected(folder, False)
    assert folder not in session.selection().selected


def test_entries_matches_naive_model_under_selection(root: Path, guard: SafetyGuard) -> None:
    session = _session(root, guard)
    session.initial_scan()
    session.select(root / "旧资料")
    session.select(root / "微信文件")

    expected = naive_scan(
        root, frozenset({root / "旧资料", root / "微信文件"})
    )

    assert {e.path for e in session.entries()} == expected


# ---------------------------------------------------------------------------
# expand
# ---------------------------------------------------------------------------


def test_expand_reveals_children_without_selecting(root: Path, guard: SafetyGuard) -> None:
    session = _session(root, guard)
    session.initial_scan()

    children = session.expand(root / "旧资料")

    assert {c.name for c in children} == {"更深"}
    assert session.selection().selected == set()
    assert session.scope() is ScanScope.TOP_LEVEL_ONLY


def test_expand_reflects_selection_state(root: Path, guard: SafetyGuard) -> None:
    session = _session(root, guard)
    session.initial_scan()
    deeper = root / "旧资料" / "更深"
    session.select(deeper)

    children = session.expand(root / "旧资料")

    assert [c.selected for c in children] == [True]


def test_expand_is_cached(root: Path, guard: SafetyGuard) -> None:
    session = _session(root, guard)
    session.initial_scan()
    folder = root / "旧资料"

    session.expand(folder)
    session.expand(folder)

    assert session.read_count(folder) == 1


# ---------------------------------------------------------------------------
# 错误隔离
# ---------------------------------------------------------------------------


def test_unstatable_entry_is_recorded_but_does_not_stop_scan(
    tmp_path: Path, guard: SafetyGuard, monkeypatch: pytest.MonkeyPatch
) -> None:
    """需求 2.16 与属性 5：失败条目 error 非空，其余条目照常收集。"""
    r = build_tree(tmp_path / "r", {"好.txt": "x", "坏.txt": "y", "也好.txt": "z"})

    real = scanner_module._stat_of

    def flaky(entry: os.DirEntry[str]) -> os.stat_result | None:
        if entry.name == "坏.txt":
            return None
        return real(entry)

    monkeypatch.setattr(scanner_module, "_stat_of", flaky)
    session = _session(r, guard)

    result = session.initial_scan()
    by_name = {e.name: e for e in result.entries}

    assert set(by_name) == {"好.txt", "坏.txt", "也好.txt"}
    assert by_name["坏.txt"].error
    assert by_name["好.txt"].error is None
    assert any(err.path.name == "坏.txt" for err in result.errors)


def test_unreadable_directory_is_recorded(
    tmp_path: Path, guard: SafetyGuard, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = build_tree(tmp_path / "r", {"a.txt": "x"})
    real_scandir = os.scandir

    def deny(path: object, *args: object, **kw: object) -> object:
        if str(path) == str(r):
            raise PermissionError("拒绝访问")
        return real_scandir(path, *args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(scanner_module.os, "scandir", deny)
    session = _session(r, guard)

    result = session.initial_scan()

    assert result.entries == []
    assert [e.stage for e in result.errors] == ["listdir"]


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------


def test_cancel_discards_initial_scan_results(tmp_path: Path, guard: SafetyGuard) -> None:
    """需求 2.18：取消即丢弃结果。半个范围的方案比没有方案更危险。"""
    spec = {f"f{i:04d}.txt": "x" for i in range(200)}
    r = build_tree(tmp_path / "r", spec)
    session = _session(r, guard)
    token = CancelToken()
    token.cancel()

    result = session.initial_scan(cancel=token)

    assert result.cancelled is True
    assert result.entries == []
    assert result.subfolders == []
    assert session.entries() == []


def test_cancel_during_select_leaves_selection_untouched(
    tmp_path: Path, guard: SafetyGuard
) -> None:
    spec = {"a.txt": "x", "大目录": {f"f{i:04d}.txt": "y" for i in range(200)}}
    r = build_tree(tmp_path / "r", spec)
    session = _session(r, guard)
    session.initial_scan()

    token = CancelToken()
    token.cancel()
    delta = session.select(r / "大目录", cancel=token)

    assert delta.cancelled is True
    assert delta.added == []
    assert session.selection().selected == set()
    assert session.scope() is ScanScope.TOP_LEVEL_ONLY


def test_uncancelled_token_completes_normally(root: Path, guard: SafetyGuard) -> None:
    session = _session(root, guard)

    result = session.initial_scan(cancel=CancelToken())

    assert result.cancelled is False
    assert result.entries


# ---------------------------------------------------------------------------
# 进度
# ---------------------------------------------------------------------------


def test_progress_is_reported_and_finished(root: Path, guard: SafetyGuard) -> None:
    seen: list[ProgressSnapshot] = []
    session = _session(root, guard, progress_interval_ms=0)

    session.initial_scan(on_progress=seen.append)

    assert seen, "应当有进度通知"
    assert seen[-1].final is True
    assert {s.phase for s in seen} <= {
        scanner_module.PHASE_ENTRIES,
        scanner_module.PHASE_SUBFOLDERS,
    }


def test_progress_counts_are_monotonic(root: Path, guard: SafetyGuard) -> None:
    seen: list[ProgressSnapshot] = []
    session = _session(root, guard, progress_interval_ms=0)

    session.initial_scan(on_progress=seen.append)

    counts = [s.processed for s in seen]
    assert counts == sorted(counts)


def test_no_progress_sink_is_fine(root: Path, guard: SafetyGuard) -> None:
    session = _session(root, guard)

    assert session.initial_scan(on_progress=None).entries
