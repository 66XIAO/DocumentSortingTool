"""可复用控件。

本包内不得直接 import qfluentwidgets，组件统一从 app.ui.theme 取。

plan_tree_model 用自定义 QAbstractItemModel 而非 QTreeWidget：10 万条目下
QTreeWidget 的 item 对象开销与构建时间都无法满足 100ms 响应（需求 10.13、10.14）。
"""
