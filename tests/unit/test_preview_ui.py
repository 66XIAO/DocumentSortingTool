"""PlanTreeModel、筛选代理与预览页的测试。

重点在两处：
- 模型的懒加载确实是懒的（属性 10.13、10.14 的实现前提）
- 预览页把方案原样呈现，且危险开关都有二次确认（需求 9.6、20.4）
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QModelIndex, Qt

from app.config.settings import Settings
from app.core.models import ConflictKind, ConflictPolicy
from app.core.planner import Planner
from app.core.safety import SafetyGuard
from app.core.scanner import ScanSession
from app.ui.widgets.plan_filter import PlanFilterProxy
from app.ui.widgets.plan_tree_model import (
    FETCH_BATCH,
    ROLE_IS_GROUP,
    Column,
    PlanTreeModel,
    ViewMode,
)
from tests.fixtures.trees import build_tree

pytest.importorskip("pytestqt", reason="需要 pytest-qt")


@pytest.fixture
def guard() -> SafetyGuard:
    return SafetyGuard(system_roots=(), denied_segments=())


def _plan(root: Path, guard: SafetyGuard):
    session = ScanSession(root, guard=guard)
    session.initial_scan()
    return Planner(guard=guard).build(
        session.entries(), root=root, check_locked=False
    )


@pytest.fixture
def sample(tmp_path: Path, guard: SafetyGuard):
    root = build_tree(
        tmp_path / "下载",
        {
            "发票_2024.pdf": "a",
            "报告.pdf": "b",
            "图.png": "c",
            "神秘.zzz": "d",
        },
    )
    return root, _plan(root, guard)


# ---------------------------------------------------------------------------
# PlanTreeModel
# ---------------------------------------------------------------------------


def test_groups_by_category_in_after_mode(qtbot, sample) -> None:
    _root, result = sample
    model = PlanTreeModel()
    model.set_plan(result.plan)

    labels = set(model.group_labels())

    assert "财务/发票" in labels
    assert "文档/PDF" in labels
    assert "图片" in labels
    assert "_未分类" in labels


def test_groups_by_source_dir_in_before_mode(qtbot, sample) -> None:
    root, result = sample
    model = PlanTreeModel()
    model.set_plan(result.plan)

    model.set_mode(ViewMode.BEFORE)

    assert model.group_labels() == [str(root)]


def test_mode_switch_keeps_item_count(qtbot, sample) -> None:
    """需求 10.4：切换视图只换分组，不重建数据。"""
    _root, result = sample
    model = PlanTreeModel()
    model.set_plan(result.plan)

    def total_children() -> int:
        count = 0
        for row in range(model.rowCount()):
            parent = model.index(row, 0)
            while model.canFetchMore(parent):
                model.fetchMore(parent)
            count += model.rowCount(parent)
        return count

    after = total_children()
    model.set_mode(ViewMode.BEFORE)
    before = total_children()

    assert after == before == 4


def test_children_are_not_loaded_until_fetched(qtbot, sample) -> None:
    """懒加载：没 fetch 之前类目下是 0 行，但 hasChildren 为真。"""
    _root, result = sample
    model = PlanTreeModel()
    model.set_plan(result.plan)
    parent = model.index(0, 0)

    assert model.rowCount(parent) == 0
    assert model.hasChildren(parent)
    assert model.canFetchMore(parent)

    model.fetchMore(parent)

    assert model.rowCount(parent) > 0


def test_fetch_more_appends_in_batches(qtbot, tmp_path: Path, guard: SafetyGuard) -> None:
    count = FETCH_BATCH + 30
    root = build_tree(tmp_path / "下载", {f"f{i:04d}.pdf": "x" for i in range(count)})
    model = PlanTreeModel()
    model.set_plan(_plan(root, guard).plan)
    parent = model.index(0, 0)

    model.fetchMore(parent)
    assert model.rowCount(parent) == FETCH_BATCH
    assert model.canFetchMore(parent)

    model.fetchMore(parent)
    assert model.rowCount(parent) == count
    assert not model.canFetchMore(parent)


def test_group_rows_are_marked(qtbot, sample) -> None:
    _root, result = sample
    model = PlanTreeModel()
    model.set_plan(result.plan)
    parent = model.index(0, 0)
    model.fetchMore(parent)

    assert model.data(parent, ROLE_IS_GROUP) is True
    assert model.data(model.index(0, 0, parent), ROLE_IS_GROUP) is False


def test_item_at_returns_plan_item(qtbot, sample) -> None:
    _root, result = sample
    model = PlanTreeModel()
    model.set_plan(result.plan)
    parent = model.index(0, 0)
    model.fetchMore(parent)

    item = model.item_at(model.index(0, 0, parent))

    assert item is not None
    assert item.reason


def test_check_state_toggles_included(qtbot, sample) -> None:
    """需求 10.10。"""
    _root, result = sample
    model = PlanTreeModel()
    model.set_plan(result.plan)
    parent = model.index(0, 0)
    model.fetchMore(parent)
    index = model.index(0, Column.NAME, parent)

    assert model.data(index, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
    assert model.setData(index, Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)

    item = model.item_at(index)
    assert item is not None and item.included is False


def test_renamed_item_shows_old_and_new_name(
    qtbot, tmp_path: Path, guard: SafetyGuard
) -> None:
    """需求 10.6。"""
    root = build_tree(
        tmp_path / "下载", {"a.pdf": "x", "文档": {"PDF": {"a.pdf": "占位"}}}
    )
    session = ScanSession(root, guard=guard)
    session.initial_scan()
    result = Planner(guard=guard).build(
        session.entries(), root=root, check_locked=False
    )
    model = PlanTreeModel()
    model.set_plan(result.plan)
    parent = model.index(0, 0)
    model.fetchMore(parent)

    text = model.data(model.index(0, Column.NAME, parent), Qt.ItemDataRole.DisplayRole)

    assert "→" in text


def test_conflict_row_gets_decoration(qtbot, tmp_path: Path, guard: SafetyGuard) -> None:
    """需求 10.5。"""
    root = build_tree(
        tmp_path / "下载", {"a.pdf": "x", "文档": {"PDF": {"a.pdf": "占位"}}}
    )
    session = ScanSession(root, guard=guard)
    session.initial_scan()
    result = Planner(guard=guard).build(
        session.entries(), root=root, check_locked=False
    )
    model = PlanTreeModel()
    model.set_plan(result.plan)
    parent = model.index(0, 0)
    model.fetchMore(parent)
    index = model.index(0, Column.NAME, parent)

    item = model.item_at(index)
    assert item is not None and item.conflict is ConflictKind.EXISTS
    assert model.data(index, Qt.ItemDataRole.DecorationRole) is not None


def test_tooltip_contains_both_paths(qtbot, sample) -> None:
    _root, result = sample
    model = PlanTreeModel()
    model.set_plan(result.plan)
    parent = model.index(0, 0)
    model.fetchMore(parent)

    tooltip = model.data(
        model.index(0, Column.NAME, parent), Qt.ItemDataRole.ToolTipRole
    )

    assert "原路径" in tooltip
    assert "目标" in tooltip


def test_empty_plan_yields_no_rows(qtbot) -> None:
    model = PlanTreeModel()
    model.set_plan(None)

    assert model.rowCount() == 0
    assert model.item_at(QModelIndex()) is None


def test_headers_are_present(qtbot) -> None:
    model = PlanTreeModel()

    assert model.columnCount() == 3
    assert model.headerData(
        Column.NAME, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole
    )


# ---------------------------------------------------------------------------
# 筛选
# ---------------------------------------------------------------------------


def _load_all(model: PlanTreeModel) -> None:
    for row in range(model.rowCount()):
        parent = model.index(row, 0)
        while model.canFetchMore(parent):
            model.fetchMore(parent)


def test_filter_inactive_by_default(qtbot, sample) -> None:
    _root, result = sample
    model = PlanTreeModel()
    model.set_plan(result.plan)
    proxy = PlanFilterProxy()
    proxy.setSourceModel(model)

    assert proxy.active is False
    assert proxy.rowCount() == model.rowCount()


def test_search_filters_by_name(qtbot, sample) -> None:
    _root, result = sample
    model = PlanTreeModel()
    model.set_plan(result.plan)
    _load_all(model)
    proxy = PlanFilterProxy()
    proxy.setSourceModel(model)

    proxy.set_query("发票")

    assert proxy.active is True
    visible = _visible_names(proxy)
    assert visible == {"发票_2024.pdf"}


def test_only_unclassified_filter(qtbot, sample) -> None:
    _root, result = sample
    model = PlanTreeModel()
    model.set_plan(result.plan)
    _load_all(model)
    proxy = PlanFilterProxy()
    proxy.setSourceModel(model)

    proxy.set_only_unclassified(True)

    assert _visible_names(proxy) == {"神秘.zzz"}


def _visible_names(proxy: PlanFilterProxy) -> set[str]:
    names: set[str] = set()
    for row in range(proxy.rowCount()):
        parent = proxy.index(row, 0)
        for child in range(proxy.rowCount(parent)):
            index = proxy.index(child, Column.NAME, parent)
            source = proxy.mapToSource(index)
            model = proxy.sourceModel()
            item = model.item_at(source)  # type: ignore[attr-defined]
            if item is not None:
                names.add(item.entry.name)
    return names


# ---------------------------------------------------------------------------
# 预览页
# ---------------------------------------------------------------------------


def test_preview_page_shows_stats(qtbot, sample) -> None:
    from app.ui.pages.preview_page import PreviewPage

    _root, result = sample
    page = PreviewPage()
    qtbot.addWidget(page)

    page.show_result(result)

    assert page.model.rowCount() > 0
    assert page.category_list.all_parts()


def test_preview_page_default_policy(qtbot) -> None:
    from app.ui.pages.preview_page import PreviewPage

    page = PreviewPage()
    qtbot.addWidget(page)

    assert page.conflict_policy() is ConflictPolicy.AUTO_RENAME
    assert page.cleanup_enabled() is False


def test_execute_disabled_until_plan_arrives(qtbot) -> None:
    """没有方案时不该能点「开始整理」。"""
    from app.ui.pages.preview_page import PreviewPage

    page = PreviewPage()
    qtbot.addWidget(page)

    assert page._execute.isEnabled() is False


def test_execute_blocked_when_space_insufficient(qtbot, sample) -> None:
    """需求 9.10：空间不足整体阻止执行。"""
    from app.core.planner import SpaceCheck
    from app.ui.pages.preview_page import PreviewPage

    _root, result = sample
    result.space = SpaceCheck(ok=False, volume="D:\\", required=100, free=1)
    page = PreviewPage()
    qtbot.addWidget(page)

    page.show_result(result)

    assert page._execute.isEnabled() is False


def test_ai_switch_disabled_without_provider(qtbot) -> None:
    """需求 6.3。"""
    from app.ui.pages.preview_page import PreviewPage

    page = PreviewPage()
    qtbot.addWidget(page)

    page.set_ai_available(False)

    assert page._ai_switch.isEnabled() is False


def test_override_report_is_displayed(qtbot, sample) -> None:
    from app.core.overrides import ApplyReport
    from app.ui.pages.preview_page import PreviewPage

    _root, result = sample
    result.overrides = ApplyReport(kept=4, dropped_paths=["x"])
    page = PreviewPage()
    qtbot.addWidget(page)

    page.show_result(result)

    assert "4" in page._overrides_label.text()
    assert page._clear_overrides.isEnabled() is True


def test_flow_reaches_preview_after_plan(qtbot, tmp_path: Path, guard: SafetyGuard) -> None:
    """选目录 → 扫描 → 生成方案：走到预览页并显示统计。"""
    from app.services.plan_service import PlanService
    from app.services.scan_service import ScanService
    from app.ui.main_window import STEP_PREVIEW, SortFlowPage

    root = build_tree(
        tmp_path / "下载", {"发票.pdf": "a", "报告.docx": "b", "神秘.zzz": "c"}
    )
    scan = ScanService(guard=guard)
    plan = PlanService(guard=guard)
    flow = SortFlowPage(scan, Settings(), plan)
    qtbot.addWidget(flow)

    flow.select_page._set_root(root)
    with qtbot.waitSignal(scan.finished, timeout=15000):
        flow.select_page._emit_scan()

    with qtbot.waitSignal(plan.finished, timeout=15000) as blocker:
        flow.scan_page.proceedRequested.emit()

    assert flow._stack.currentIndex() == STEP_PREVIEW
    assert blocker.args[0].plan.stats().total_files == 3
    assert flow.preview_page.model.rowCount() > 0

    for service in (scan, plan):
        service.cancel()
        service.wait(5000)


def test_item_exclusion_is_recorded_as_override(
    qtbot, tmp_path: Path, guard: SafetyGuard
) -> None:
    """勾掉一项必须记进 override，否则重算就丢了。"""
    from app.services.plan_service import PlanService
    from app.services.scan_service import ScanService
    from app.ui.main_window import SortFlowPage

    root = build_tree(tmp_path / "下载", {"a.pdf": "x"})
    scan = ScanService(guard=guard)
    plan = PlanService(guard=guard)
    flow = SortFlowPage(scan, Settings(), plan)
    qtbot.addWidget(flow)

    flow._set_item_included(root / "a.pdf", False)

    assert plan.overrides.items[str(root / "a.pdf")].included is False
