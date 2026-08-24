"""执行确认对话框必须列出将被删除的空目录。需求 20.7。

原实现只显示文件数与文件夹数，用户看不到「还会删掉哪几个子文件夹」——而这是需求 20
唯一会改动目录结构的动作。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ui.theme.components import ListConfirmDialog

pytest.importorskip("pytestqt", reason="需要 pytest-qt 才能驱动 Qt 事件循环")


@pytest.fixture
def host(qtbot):
    from PySide6.QtWidgets import QWidget

    widget = QWidget()
    qtbot.addWidget(widget)
    return widget


def test_dialog_lists_every_item(host) -> None:
    """完整清单：一条都不能少（需求 20.7 要的是完整，不是摘要）。"""
    items = [f"子目录{i}" for i in range(120)]

    dialog = ListConfirmDialog(
        host, "开始整理", "将删除 120 个目录", items, ok_text="开始整理"
    )

    assert dialog.list_widget.count() == len(items)
    shown = [dialog.list_widget.item(i).text() for i in range(dialog.list_widget.count())]
    assert shown == items


def test_dialog_height_is_bounded(host) -> None:
    """清单再长也不能把对话框撑过屏幕——与主窗口那次越界是同一类故障。"""
    items = [f"很长的子目录名字_{i}" for i in range(500)]

    dialog = ListConfirmDialog(host, "开始整理", "正文", items, ok_text="开始整理")

    assert dialog.list_widget.maximumHeight() <= ListConfirmDialog.MAX_LIST_HEIGHT
    assert dialog.sizeHint().height() <= 720


def test_cancel_is_the_default_button(host) -> None:
    """这个对话框只在会删目录时出现，回车不该落在破坏性动作上。"""
    dialog = ListConfirmDialog(host, "开始整理", "正文", ["a"], ok_text="开始整理")

    assert dialog.cancel_button.isDefault() is True
    assert dialog.ok_button.isDefault() is False


def test_confirmed_is_false_until_ok_clicked(host) -> None:
    dialog = ListConfirmDialog(host, "开始整理", "正文", ["a"], ok_text="开始整理")
    assert dialog.confirmed is False

    dialog.cancel_button.click()
    assert dialog.confirmed is False


def test_confirmed_is_true_after_ok(host) -> None:
    dialog = ListConfirmDialog(host, "开始整理", "正文", ["a"], ok_text="开始整理")

    dialog.ok_button.click()

    assert dialog.confirmed is True


def test_confirm_with_list_falls_back_when_empty(host, monkeypatch) -> None:
    """没有目录要删时不该弹一个空列表出来，退回普通确认框。"""
    from app.ui.theme import components

    seen: dict[str, object] = {}

    def fake_confirm(parent, title, body, ok_text, cancel_text="取消") -> bool:
        seen["called"] = True
        return True

    monkeypatch.setattr(components, "confirm", fake_confirm)

    result = components.confirm_with_list(
        host, "开始整理", "正文", [], ok_text="开始整理"
    )

    assert result is True
    assert seen.get("called") is True


# ---------------------------------------------------------------------------
# 主窗口把预测清单接进确认框
# ---------------------------------------------------------------------------


def test_display_path_is_relative_to_root() -> None:
    """清单显示相对路径：几十条绝对路径都带同一段前缀，看不出差别。"""
    from app.ui.main_window import SortFlowPage

    root = Path("C:/data/root")

    assert SortFlowPage._display_path(root / "甲" / "乙", root) == str(Path("甲/乙"))


def test_display_path_falls_back_to_absolute_when_outside() -> None:
    from app.ui.main_window import SortFlowPage

    root = Path("C:/data/root")
    outside = Path("D:/elsewhere/x")

    assert SortFlowPage._display_path(outside, root) == str(outside)
