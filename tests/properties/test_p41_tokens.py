"""属性 41：视觉令牌取值合规。

需求 17.4-17.8。零 Qt 依赖，直接断言取值。
"""

from __future__ import annotations

from app.ui.theme import tokens
from app.core.models import CATEGORY_PALETTE, UNCLASSIFIED_COLOR, category_color


def test_primary_color() -> None:
    """需求 17.4：主色 #2563EB。"""
    assert tokens.PRIMARY == "#2563EB"


def test_radius() -> None:
    """需求 17.4：圆角 8px。"""
    assert tokens.RADIUS == 8


def test_spacing_grid() -> None:
    """需求 17.4：间距栅格 4/8/12/16/24。"""
    assert tokens.SPACING == (4, 8, 12, 16, 24)
    # 所有命名常量都在栅格内
    assert tokens.SPACE_XS == 4
    assert tokens.SPACE_SM == 8
    assert tokens.SPACE_MD == 12
    assert tokens.SPACE_LG == 16
    assert tokens.SPACE_XL == 24


def test_status_colors() -> None:
    """需求 17.5：状态色。"""
    assert tokens.SUCCESS == "#16A34A"
    assert tokens.WARNING == "#F59E0B"
    assert tokens.DANGER == "#DC2626"


def test_font_families() -> None:
    """需求 17.6：Segoe UI + 微软雅黑 UI。"""
    assert "Segoe UI" in tokens.FONT_FAMILIES
    assert "Microsoft YaHei UI" in tokens.FONT_FAMILIES


def test_category_palette_has_ten_colors() -> None:
    """需求 17.8：类目 chip 10 色柔和色板。"""
    assert len(CATEGORY_PALETTE) == 10
    # 所有颜色都是有效的十六进制
    for color in CATEGORY_PALETTE:
        assert color.startswith("#")
        assert len(color) == 7


def test_category_color_wraps() -> None:
    """需求 17.8：类目 chip 10 色轮转取色。"""
    assert category_color(0) == category_color(10)
    assert category_color(1) == category_color(11)
    assert category_color(0) == CATEGORY_PALETTE[0]


def test_unclassified_color() -> None:
    """_未分类 固定用中性灰。"""
    assert UNCLASSIFIED_COLOR == "#94A3B8"


def test_qss_spacing_values_in_grid() -> None:
    """样式表里的间距数值全部落在栅格内。"""
    from app.ui.theme.qss import app_stylesheet
    import re

    qss = app_stylesheet()
    # 提取 padding/margin/border-radius 后的 px 数值（字号 font-size 除外）
    # 匹配 padding、margin、border-radius 属性后的数值
    spacing_pattern = r"(?:padding|margin|border-radius):\s*(\d+)px"
    spacing_values = set(int(m) for m in re.findall(spacing_pattern, qss))
    grid = set(tokens.SPACING)
    assert spacing_values.issubset(grid), f"间距数值 {spacing_values - grid} 不在栅格内"
