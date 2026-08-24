"""主窗口几何：窗口必须完整落在屏幕可用区内，标题栏必须可拖动。

这组测试守的是一条曾经真实发生的故障：设置页把六组选项直接铺在页面上，叠起来约
940px 高；所有页面共用一个 StackedWidget，栈的最小高度取各页最大值，于是整个主
窗口被顶到 1000px 以上。在 1536x864（可用高 816）的屏幕上窗口随后被居中，实测
geometry 为 (441, -389, 1000, 1127)——标题栏跑到屏幕上方之外，用户既不能拖动窗口
也够不到关闭按钮。

因此这里断言两件事：
    1. 任何单个页面都不得独自把窗口最小高度顶过常见屏幕的可用高度
    2. fit_to_screen() 之后窗口框必须完整落在可用区内，且左上角不越界
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QScrollArea

from app.config.settings import SettingsManager

pytest.importorskip("pytestqt", reason="需要 pytest-qt 才能驱动 Qt 事件循环")

#: 最常见的小屏可用高度：768px 屏幕减去任务栏。窗口最小高度必须低于它，
#: 否则在这类机器上永远无法完整显示。
SMALL_SCREEN_AVAILABLE_HEIGHT = 720


@pytest.fixture
def manager(tmp_path: Path) -> SettingsManager:
    return SettingsManager(base_dir=tmp_path / "cfg")


@pytest.fixture
def window(qtbot, manager: SettingsManager):
    from app.ui.main_window import MainWindow

    win = MainWindow(manager)
    qtbot.addWidget(win)
    return win


# ---------------------------------------------------------------------------
# 页面不得独自顶高窗口
# ---------------------------------------------------------------------------


def test_no_page_forces_window_taller_than_small_screen(window) -> None:
    """任何一页的最小高度都必须能塞进 768px 屏幕的可用区。"""
    pages = {
        "sort_flow": window.sort_flow,
        "history_page": window.history_page,
        "rules_page": window.rules_page,
        "settings_page": window.settings_page,
    }
    too_tall = {
        name: page.minimumSizeHint().height()
        for name, page in pages.items()
        if page.minimumSizeHint().height() > SMALL_SCREEN_AVAILABLE_HEIGHT
    }

    assert not too_tall, f"这些页面会把窗口顶出屏幕: {too_tall}"


def test_window_minimum_height_fits_small_screen(window) -> None:
    """窗口整体的最小高度也要能塞进 768px 屏幕。"""
    assert window.minimumSizeHint().height() <= SMALL_SCREEN_AVAILABLE_HEIGHT
    assert window.minimumHeight() <= SMALL_SCREEN_AVAILABLE_HEIGHT


def test_settings_page_is_scrollable(window) -> None:
    """设置项必须放在滚动区里——这是它不再顶高窗口的原因。"""
    page = window.settings_page

    assert page.findChildren(QScrollArea), "设置页必须有滚动区"


def test_settings_save_button_stays_outside_scroll_area(window) -> None:
    """「保存」留在滚动区外：改完最后一项不该还要往上滚才能提交。"""
    page = window.settings_page
    scroll = page.findChildren(QScrollArea)[0]
    button = page._save_btn

    assert button.isAncestorOf is not None
    assert not scroll.isAncestorOf(button)


# ---------------------------------------------------------------------------
# fit_to_screen 的夹紧
# ---------------------------------------------------------------------------


def _available():
    screen = QGuiApplication.primaryScreen()
    assert screen is not None
    return screen.availableGeometry()


def test_window_fits_available_area_after_construction(window) -> None:
    available = _available()

    window.fit_to_screen()

    frame = window.frameGeometry()
    assert frame.width() <= available.width()
    assert frame.height() <= available.height()


def test_title_bar_is_never_above_screen_top(window) -> None:
    """标题栏被推到屏幕上方之外时用户连拖动窗口都做不到，这是最严重的形态。"""
    available = _available()

    # 先人为制造故障现场：把窗口挪到屏幕上方之外
    window.move(available.left() + 100, available.top() - 400)
    window.fit_to_screen()

    assert window.frameGeometry().top() >= available.top()


def test_left_edge_is_never_off_screen(window) -> None:
    available = _available()

    window.move(available.left() - 500, available.top() + 50)
    window.fit_to_screen()

    assert window.frameGeometry().left() >= available.left()


def test_oversized_window_is_shrunk_into_available_area(window) -> None:
    """窗口比屏幕还大时必须缩回来，而不是让右下角或标题栏溢出。"""
    available = _available()

    window.resize(available.width() + 800, available.height() + 800)
    window.fit_to_screen()

    frame = window.frameGeometry()
    assert frame.width() <= available.width()
    assert frame.height() <= available.height()
    assert frame.top() >= available.top()
    assert frame.left() >= available.left()


def test_minimum_size_is_relaxed_so_resize_can_take_effect(window) -> None:
    """最小尺寸必须跟着放宽，否则 resize 会被它挡住、夹紧无效。"""
    available = _available()

    window.fit_to_screen()

    assert window.minimumWidth() <= available.width()
    assert window.minimumHeight() <= available.height()


def test_fit_to_screen_is_idempotent(window) -> None:
    window.fit_to_screen()
    first = window.frameGeometry()

    window.fit_to_screen()

    assert window.frameGeometry() == first
