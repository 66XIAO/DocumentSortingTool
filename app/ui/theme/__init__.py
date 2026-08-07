"""主题层：设计令牌 + qfluentwidgets 组件再导出。

这是全应用**唯一**允许 import qfluentwidgets 的位置。pages 与 widgets 必须
从这里取组件与令牌，不得直连组件库，由 tests/unit/test_layering.py 静态守卫。

理由：qfluentwidgets 在 PyPI 标注 GPLv3（作者另售商业授权），若后续因分发方式
需要更换组件库，改动面收敛在本层，pages 与 widgets 不必改动。

tokens      设计令牌（主色、圆角、间距栅格、状态色、字体族、类目色板），零 Qt 依赖
components  组件再导出 + 确认对话框 / 提示条 / 主题应用的统一入口
qss         补充样式表
"""

from app.ui.theme.components import (
    FLUENT_AVAILABLE,
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ChoiceDialog,
    ComboBox,
    CountdownDialog,
    FluentWindow,
    IndeterminateProgressBar,
    LineEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
    SwitchButton,
    TitleLabel,
    TreeWidget,
    apply_system_theme,
    choose_option,
    confirm,
    countdown_to_start,
    toast_error,
    toast_info,
)
from app.ui.theme.qss import app_stylesheet
from app.ui.theme.tokens import (
    CATEGORY_PALETTE,
    CLEANUP_SWITCH_LABEL,
    CONFLICT,
    DANGER,
    FONT_FAMILIES,
    PRIMARY,
    RADIUS,
    SPACE_LG,
    SPACE_MD,
    SPACE_SM,
    SPACE_XL,
    SPACE_XS,
    SPACING,
    SUBFOLDER_PICK_CONSEQUENCE,
    SUCCESS,
    UNCLASSIFIED_COLOR,
    WARNING,
    category_color,
)

__all__ = [
    "CATEGORY_PALETTE",
    "CLEANUP_SWITCH_LABEL",
    "CONFLICT",
    "DANGER",
    "FLUENT_AVAILABLE",
    "FONT_FAMILIES",
    "PRIMARY",
    "RADIUS",
    "SPACE_LG",
    "SPACE_MD",
    "SPACE_SM",
    "SPACE_XL",
    "SPACE_XS",
    "SPACING",
    "SUBFOLDER_PICK_CONSEQUENCE",
    "SUCCESS",
    "UNCLASSIFIED_COLOR",
    "WARNING",
    "BodyLabel",
    "CaptionLabel",
    "CheckBox",
    "ChoiceDialog",
    "ComboBox",
    "CountdownDialog",
    "FluentWindow",
    "IndeterminateProgressBar",
    "LineEdit",
    "PrimaryPushButton",
    "ProgressBar",
    "PushButton",
    "StrongBodyLabel",
    "SubtitleLabel",
    "SwitchButton",
    "TitleLabel",
    "TreeWidget",
    "app_stylesheet",
    "apply_system_theme",
    "category_color",
    "choose_option",
    "confirm",
    "countdown_to_start",
    "toast_error",
    "toast_info",
]
