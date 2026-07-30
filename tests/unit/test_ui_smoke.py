"""UI 冒烟与令牌合规测试。

设计明确不对 UI 做像素级断言（属性 41 只要求「令牌取值与控件存在性」）。这里覆盖
三件事：

    设计令牌的取值合规（属性 41）
    主窗口与各页面能被构造出来（需求 17.1、17.2）
    选目录 -> 扫描 -> 勾选子文件夹这条主链路在真实 Qt 事件循环里能跑通

最后一条是「能不能初步验证」的实际依据——它替代了手工点一遍。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config.settings import Settings, SettingsManager
from app.core.models import RejectReason, ScanOptions
from app.core.safety import SafetyGuard
from app.services.scan_service import ScanService
from app.ui.theme import qss, tokens
from tests.fixtures.trees import build_tree

pytest.importorskip("pytestqt", reason="需要 pytest-qt 才能驱动 Qt 事件循环")

TREE = {
    "报告.pdf": "x",
    "发票_2024.pdf": "y",
    "旧资料": {"合同.docx": "z", "更深": {"a.txt": "w"}},
    "微信文件": {"图片.png": "p"},
}


# ---------------------------------------------------------------------------
# 属性 41：视觉令牌合规
# ---------------------------------------------------------------------------


def test_primary_colour_and_radius() -> None:
    assert tokens.PRIMARY == "#2563EB"
    assert tokens.RADIUS == 8


def test_spacing_grid_is_exactly_the_declared_set() -> None:
    assert set(tokens.SPACING) == {4, 8, 12, 16, 24}


def test_status_colours() -> None:
    assert tokens.SUCCESS == "#16A34A"
    assert tokens.WARNING == "#F59E0B"
    assert tokens.DANGER == "#DC2626"


def test_font_families() -> None:
    assert tokens.FONT_FAMILIES == ("Segoe UI", "Microsoft YaHei UI")


def test_category_palette_has_ten_colours() -> None:
    assert len(tokens.CATEGORY_PALETTE) == 10
    assert len(set(tokens.CATEGORY_PALETTE)) == 10


@pytest.mark.parametrize("index", [0, 1, 5, 9, 10, 23, 100])
def test_category_colour_rotates_every_ten(index: int) -> None:
    """属性 41：color(i) == color(i + 10)。"""
    assert tokens.category_color(index) == tokens.category_color(index + 10)
    assert tokens.category_color(index) in tokens.CATEGORY_PALETTE


def test_stylesheet_padding_values_stay_on_the_grid() -> None:
    """样式表里出现的间距数值必须落在栅格上。"""
    sheet = qss.app_stylesheet()
    spacings = {
        int(value)
        for value in re.findall(r"(?:padding|margin)[^:]*:\s*(\d+)px", sheet)
    }
    assert spacings
    assert spacings <= set(tokens.SPACING)


def test_stylesheet_uses_primary_colour() -> None:
    assert tokens.PRIMARY in qss.app_stylesheet()


def test_cleanup_switch_label_is_fixed() -> None:
    """需求 20.2 指定了这个文案。"""
    assert tokens.CLEANUP_SWITCH_LABEL == "清理整理后变空的子文件夹"


# ---------------------------------------------------------------------------
# 构造性冒烟
# ---------------------------------------------------------------------------


@pytest.fixture
def guard() -> SafetyGuard:
    return SafetyGuard(system_roots=(), denied_segments=())


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return build_tree(tmp_path / "下载", TREE)


@pytest.fixture
def manager(tmp_path: Path) -> SettingsManager:
    return SettingsManager(base_dir=tmp_path / "cfg")


def test_theme_layer_reports_component_library_state() -> None:
    from app.ui.theme import FLUENT_AVAILABLE

    assert isinstance(FLUENT_AVAILABLE, bool)


def test_state_widgets_construct(qtbot) -> None:
    from app.ui.widgets.states import EmptyState, ErrorState, SkeletonState

    empty = EmptyState("空", "提示", "动作", lambda: None)
    skeleton = SkeletonState("扫描中")
    error = ErrorState("错误", "细节", "重试", lambda: None)
    for widget in (empty, skeleton, error):
        qtbot.addWidget(widget)

    empty.set_text("新标题", "新提示")
    skeleton.set_detail("已处理 10 项")
    error.set_error("新错误", "新细节")


def test_select_page_constructs_and_reports_options(qtbot) -> None:
    from app.ui.pages.select_page import SelectPage

    page = SelectPage()
    qtbot.addWidget(page)

    assert page.root is None
    assert page.options() == ScanOptions()

    page.set_recent(["D:\\下载", "E:\\资料"])
    page.set_recent([])


def test_select_page_shows_rejection_reason(qtbot, guard: SafetyGuard) -> None:
    from app.ui.pages.select_page import SelectPage

    page = SelectPage()
    qtbot.addWidget(page)
    result = guard.check_root(Path("Z:\\不存在的盘"))

    page.show_admission(result)

    assert result.reason is RejectReason.NOT_EXISTS


def test_scan_page_constructs(qtbot, root: Path) -> None:
    from app.ui.pages.scan_page import ScanPage

    page = ScanPage()
    qtbot.addWidget(page)

    page.set_root(root)
    page.show_scanning()
    page.show_error("失败了")
    page.update_counts(3, 1)


def test_main_window_constructs_with_four_nav_entries(
    qtbot, manager: SettingsManager
) -> None:
    from app.ui.main_window import MainWindow

    window = MainWindow(manager)
    qtbot.addWidget(window)

    assert window.sort_flow is not None
    assert window.history_page is not None
    assert window.rules_page is not None
    assert window.settings_page is not None
    assert window.windowTitle() == "DocSorter 文档分类工具"


def test_main_window_persists_settings_on_close(
    qtbot, manager: SettingsManager
) -> None:
    from app.ui.main_window import MainWindow

    window = MainWindow(manager)
    qtbot.addWidget(window)
    window.close()

    assert manager.settings_path.exists()


# ---------------------------------------------------------------------------
# 主链路：选目录 -> 扫描 -> 勾选
# ---------------------------------------------------------------------------


def test_scan_flow_end_to_end(qtbot, root: Path, guard: SafetyGuard) -> None:
    """替代手工点一遍：走完扫描并勾选一个子文件夹。"""
    from app.ui.main_window import SortFlowPage

    service = ScanService(guard=guard)
    flow = SortFlowPage(service, Settings())
    qtbot.addWidget(flow)

    flow.select_page._set_root(root)
    with qtbot.waitSignal(service.finished, timeout=15000) as blocker:
        flow.select_page._emit_scan()

    result = blocker.args[0]
    assert {e.name for e in result.entries} == {"报告.pdf", "发票_2024.pdf"}
    assert {s.name for s in result.subfolders} == {"旧资料", "微信文件"}

    with qtbot.waitSignal(service.finished, timeout=15000):
        flow._toggle_subfolder(root / "旧资料", True)

    session = service.session
    assert session is not None
    assert session.selection().selected == {root / "旧资料"}
    assert any(e.name == "合同.docx" for e in session.entries())

    with qtbot.waitSignal(service.finished, timeout=15000):
        flow._toggle_subfolder(root / "旧资料", False)

    assert session.selection().selected == set()

    service.cancel()
    service.wait(5000)


def test_flow_records_recent_root(qtbot, root: Path, guard: SafetyGuard) -> None:
    from app.ui.main_window import SortFlowPage

    service = ScanService(guard=guard)
    settings = Settings()
    flow = SortFlowPage(service, settings)
    qtbot.addWidget(flow)

    flow.select_page._set_root(root)
    with qtbot.waitSignal(service.finished, timeout=15000):
        flow.select_page._emit_scan()

    assert str(root) in settings.ui.recent_roots

    service.cancel()
    service.wait(5000)


def test_flow_expands_subfolder_lazily(qtbot, root: Path, guard: SafetyGuard) -> None:
    from app.ui.main_window import SortFlowPage

    service = ScanService(guard=guard)
    flow = SortFlowPage(service, Settings())
    qtbot.addWidget(flow)

    flow.select_page._set_root(root)
    with qtbot.waitSignal(service.finished, timeout=15000):
        flow.select_page._emit_scan()

    flow._expand(root / "旧资料")

    assert service.session is not None
    assert service.session.selection().selected == set()

    service.cancel()
    service.wait(5000)


def test_flow_falls_back_to_select_page_when_root_rejected(
    qtbot, tmp_path: Path, guard: SafetyGuard
) -> None:
    from app.ui.main_window import SortFlowPage

    service = ScanService(guard=guard)
    flow = SortFlowPage(service, Settings())
    qtbot.addWidget(flow)

    missing = tmp_path / "不存在"
    flow.select_page._set_root(missing)

    with qtbot.waitSignal(service.rootRejected, timeout=3000):
        flow.select_page._emit_scan()

    assert service.session is None
