"""设计令牌。需求 17.4-17.8。

纯数据、零 Qt 依赖，因此属性 41（视觉令牌合规）可以直接断言取值而不必拉起窗口。

间距一律从 ``SPACING`` 取，不写字面量——属性 41 会扫描令牌与样式表里出现的全部
间距数值，要求它们都落在 {4, 8, 12, 16, 24} 之内。
"""

from __future__ import annotations

# -- 主色与圆角 -------------------------------------------------------------

PRIMARY = "#2563EB"
RADIUS = 8

# -- 间距栅格 ---------------------------------------------------------------

SPACING: tuple[int, ...] = (4, 8, 12, 16, 24)
SPACE_XS, SPACE_SM, SPACE_MD, SPACE_LG, SPACE_XL = SPACING

# -- 状态色 -----------------------------------------------------------------

SUCCESS = "#16A34A"
WARNING = "#F59E0B"
DANGER = "#DC2626"

#: PlanItem.conflict 不为 none 时的角标色（需求 10.5）
CONFLICT = WARNING

# -- 字体 -------------------------------------------------------------------

FONT_FAMILIES: tuple[str, ...] = ("Segoe UI", "Microsoft YaHei UI")
FONT_FAMILY_CSS = ", ".join(f'"{name}"' for name in FONT_FAMILIES)

FONT_SIZE_BODY = 14
FONT_SIZE_CAPTION = 12
FONT_SIZE_TITLE = 20
FONT_SIZE_STAT = 24

# -- 类目色板 ---------------------------------------------------------------
#
# 色板的定义在 core/models.py：``Category.color`` 是 core 字段且要随 manifest
# 落盘，core 又不能 import ui（需求 18.7）。这里只是再导出，保证界面显示的颜色
# 与落盘记录的颜色永远是同一份。

from app.core.models import (  # noqa: E402
    CATEGORY_PALETTE,
    UNCLASSIFIED_COLOR,
    category_color,
)


# -- 语义化文案（集中放置，避免同一措辞在多处漂移）-------------------------

CLEANUP_SWITCH_LABEL = "清理整理后变空的子文件夹"

SUBFOLDER_PICK_CONSEQUENCE = (
    "勾选后，该文件夹下的散落文件会被移出原文件夹，提升到根目录下的类目目录。"
    "文件夹里的子文件夹不受影响，需要单独勾选。"
)
