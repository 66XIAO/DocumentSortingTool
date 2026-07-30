"""界面层：PySide6 + qfluentwidgets。

main_window  FluentWindow + 侧边导航
pages/       select / scan / preview / result / history / rules / settings
widgets/     stat_card / category_list / plan_tree_model / detail_panel /
             action_bar / subfolder_picker / empty_state / skeleton / error_state
theme/       设计令牌 + 组件再导出层 + qss

重要约束：对 qfluentwidgets 的 import 只允许出现在 theme 层，pages 与 widgets
统一从 theme 取组件与令牌。这样若因组件库授权（GPLv3）需要更换实现，改动面
收敛在 theme 内。该约束由 tests/unit/test_layering.py 守卫。
"""
