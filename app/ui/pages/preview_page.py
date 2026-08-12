"""方案预览页。需求 10、6.2、9.6、19.8-19.10、20.2-20.4。

这一页是「误点即打散上千文件」这个风险的主要拦截点，所以它的重心不是好看，而是
**让用户在执行前看清每个文件会去哪、为什么**：

- 统计卡把「待移动 / 冲突 / 未分类」摊在最上面，不用展开树也能看到规模
- 每一行都带命中理由，冲突项打橙色角标
- 底部操作条把「模拟运行」放在「开始整理」左边，且前者是常规按钮、后者是主按钮
- 空目录清理开关从 false 切到 true 时弹警告（需求 20.4）
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QRadioButton,
    QSplitter,
    QStackedWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from app.core.models import ConflictPolicy, PlanItem, SortPlan
from app.core.overrides import ApplyReport
from app.core.planner import PlanResult
from app.ui.theme import (
    CLEANUP_SWITCH_LABEL,
    CONFLICT,
    DANGER,
    SPACE_LG,
    SPACE_MD,
    SPACE_SM,
    SPACE_XL,
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
    SwitchButton,
    TitleLabel,
    confirm,
)
from app.ui.widgets.category_list import CategoryList
from app.ui.widgets.detail_panel import DetailPanel
from app.ui.widgets.plan_filter import PlanFilterProxy
from app.ui.widgets.plan_tree_model import Column, PlanTreeModel, ViewMode
from app.ui.widgets.states import EmptyState, ErrorState

_POLICY_LABELS: tuple[tuple[str, ConflictPolicy], ...] = (
    ("同名时自动改名", ConflictPolicy.AUTO_RENAME),
    ("同名时跳过", ConflictPolicy.SKIP),
    ("同名时覆盖（危险）", ConflictPolicy.OVERWRITE),
)


class _StatTile(QWidget):
    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._value = StrongBodyLabel("—", self)
        self._value.setObjectName("statValue")
        self._label = CaptionLabel(label, self)
        self._label.setObjectName("statLabel")
        layout.addWidget(self._value)
        layout.addWidget(self._label)

    def set_value(self, text: str, warn: bool = False) -> None:
        self._value.setText(text)
        self._value.setStyleSheet(f"color: {CONFLICT};" if warn else "")


class PreviewPage(QWidget):
    """第三步：看清并修改方案。"""

    dryRunRequested = Signal()
    executeRequested = Signal()
    conflictPolicyChanged = Signal(object)
    cleanupToggled = Signal(bool)
    aiToggled = Signal(bool)
    clearOverridesRequested = Signal()
    backRequested = Signal()

    # 类目编辑（转发给 PlanService 记入 override）
    renameCategory = Signal(object, object)
    recolorCategory = Signal(object, str)
    mergeCategory = Signal(object, object)
    createCategory = Signal(object)
    deleteCategory = Signal(object)
    itemIncludedChanged = Signal(object, bool)
    #: 拖拽改类目（需求 10.11）：(绝对路径列表, 目标类目 path_parts)
    itemsCategoryChanged = Signal(list, tuple)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("previewPage")
        self._plan: SortPlan | None = None
        self._cleanup_warned_off = True

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE_XL, SPACE_LG, SPACE_XL, SPACE_LG)
        outer.setSpacing(SPACE_MD)

        outer.addWidget(TitleLabel("方案预览", self))
        self._subtitle = CaptionLabel(
            "下面是将要执行的操作。此刻磁盘上什么都没变——确认无误再点「开始整理」。",
            self,
        )
        self._subtitle.setWordWrap(True)
        outer.addWidget(self._subtitle)

        outer.addLayout(self._build_stats())

        # 用 QStackedWidget 在「方案内容」与「空态/错误态」之间互斥切换
        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._build_body())  # index 0: 主内容

        self._empty = EmptyState(
            "还没有分类方案",
            "返回上一步确认目录和扫描范围，然后点击「生成分类方案」。",
            parent=self,
        )
        self._stack.addWidget(self._empty)  # index 1: 空态

        self._error = ErrorState(
            "方案生成失败", "", "重试", None, self
        )
        self._stack.addWidget(self._error)  # index 2: 错误态

        outer.addWidget(self._stack, stretch=1)
        outer.addLayout(self._build_action_bar())

    # -- 顶部统计卡 -------------------------------------------------------

    def _build_stats(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(SPACE_XL)

        self._tile_total = _StatTile("文件总数", self)
        self._tile_categories = _StatTile("类目数", self)
        self._tile_pending = _StatTile("待移动", self)
        self._tile_conflicts = _StatTile("冲突", self)
        self._tile_unclassified = _StatTile("未分类", self)
        for tile in (
            self._tile_total,
            self._tile_categories,
            self._tile_pending,
            self._tile_conflicts,
            self._tile_unclassified,
        ):
            row.addWidget(tile)
        row.addStretch(1)

        ai_box = QVBoxLayout()
        ai_box.setSpacing(0)
        self._ai_switch = SwitchButton(self)
        self._set_switch_text(self._ai_switch, "智能分类")
        self._ai_switch.setEnabled(False)
        if hasattr(self._ai_switch, "checkedChanged"):
            self._ai_switch.checkedChanged.connect(self.aiToggled.emit)  # type: ignore[attr-defined]
        else:
            self._ai_switch.toggled.connect(self.aiToggled.emit)  # type: ignore[attr-defined]
        self._ai_hint = CaptionLabel("未配置模型服务", self)
        ai_box.addWidget(self._ai_switch)
        ai_box.addWidget(self._ai_hint)
        row.addLayout(ai_box)

        self._overrides_label = CaptionLabel("", self)
        row.addWidget(self._overrides_label)
        return row

    @staticmethod
    def _set_switch_text(switch: QWidget, text: str) -> None:
        """SwitchButton 与降级后的 QCheckBox API 不同，统一在这里处理。"""
        if hasattr(switch, "setOnText"):
            switch.setOnText(text)  # type: ignore[attr-defined]
            switch.setOffText(text)  # type: ignore[attr-defined]
        elif hasattr(switch, "setText"):
            switch.setText(text)  # type: ignore[attr-defined]

    # -- 三栏 -------------------------------------------------------------

    def _build_body(self) -> QWidget:
        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        self.category_list = CategoryList(splitter)
        self.category_list.renameRequested.connect(self.renameCategory)
        self.category_list.recolorRequested.connect(self.recolorCategory)
        self.category_list.mergeRequested.connect(self.mergeCategory)
        self.category_list.createRequested.connect(self.createCategory)
        self.category_list.deleteRequested.connect(self.deleteCategory)
        splitter.addWidget(self.category_list)

        splitter.addWidget(self._build_tree_pane(splitter))

        self.detail = DetailPanel(splitter)
        splitter.addWidget(self.detail)

        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 5)
        splitter.setStretchFactor(2, 3)
        return splitter

    def _build_tree_pane(self, parent: QWidget) -> QWidget:
        pane = QWidget(parent)
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_SM)

        controls = QGridLayout()
        controls.setHorizontalSpacing(SPACE_MD)

        self._mode_after = QRadioButton("整理后", pane)
        self._mode_before = QRadioButton("整理前", pane)
        self._mode_after.setChecked(True)
        group = QButtonGroup(pane)
        group.addButton(self._mode_after)
        group.addButton(self._mode_before)
        self._mode_after.toggled.connect(self._on_mode_changed)
        controls.addWidget(self._mode_after, 0, 0)
        controls.addWidget(self._mode_before, 0, 1)

        self._search = LineEdit(pane)
        self._search.setPlaceholderText("按文件名搜索")
        self._search.textChanged.connect(self._on_search)
        controls.addWidget(self._search, 0, 2)

        self._only_conflicts = CheckBox("只看冲突", pane)
        self._only_unclassified = CheckBox("只看未分类", pane)
        self._only_conflicts.toggled.connect(self._on_filter_changed)
        self._only_unclassified.toggled.connect(self._on_filter_changed)
        controls.addWidget(self._only_conflicts, 0, 3)
        controls.addWidget(self._only_unclassified, 0, 4)
        controls.setColumnStretch(2, 1)
        layout.addLayout(controls)

        self.model = PlanTreeModel(pane)
        self.proxy = PlanFilterProxy(pane)
        self.proxy.setSourceModel(self.model)

        self.tree = QTreeView(pane)
        self.tree.setModel(self.proxy)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        # 多选：拖拽改类目（需求 10.11）允许一次拖多个条目
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # InternalMove：只在树内部拖拽，且视图不会在 drop 后自行删除源行——
        # 行的增删由重算后的模型 reset 统一完成
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.tree.setDropIndicatorShown(True)
        header = self.tree.header()
        header.setSectionResizeMode(Column.NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(Column.ACTION, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(Column.REASON, QHeaderView.ResizeMode.Stretch)
        self.tree.selectionModel().currentChanged.connect(self._on_current_changed)
        self.model.dataChanged.connect(self._on_model_data_changed)
        self.model.itemsDropped.connect(self.itemsCategoryChanged)
        layout.addWidget(self.tree, stretch=1)

        self._space_warning = BodyLabel("", pane)
        self._space_warning.setWordWrap(True)
        self._space_warning.setStyleSheet(f"color: {DANGER};")
        self._space_warning.setVisible(False)
        layout.addWidget(self._space_warning)
        return pane

    # -- 底部操作条 -------------------------------------------------------

    def _build_action_bar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(SPACE_MD)

        self._back = PushButton("返回", self)
        self._back.clicked.connect(lambda: self.backRequested.emit())
        row.addWidget(self._back)

        row.addWidget(CaptionLabel("冲突处理", self))
        self._policy = ComboBox(self)
        for label, _policy in _POLICY_LABELS:
            self._policy.addItem(label)
        self._policy.currentIndexChanged.connect(self._on_policy_changed)
        row.addWidget(self._policy)

        self._cleanup = SwitchButton(self)
        self._set_switch_text(self._cleanup, CLEANUP_SWITCH_LABEL)
        self._cleanup.checkedChanged.connect(
            self._on_cleanup_changed
        ) if hasattr(self._cleanup, "checkedChanged") else self._cleanup.toggled.connect(
            self._on_cleanup_changed
        )
        row.addWidget(self._cleanup)

        self._clear_overrides = PushButton("清除全部手工调整", self)
        self._clear_overrides.clicked.connect(self._on_clear_overrides)
        self._clear_overrides.setEnabled(False)
        row.addWidget(self._clear_overrides)

        row.addStretch(1)

        self._dry_run = PushButton("模拟运行", self)
        self._dry_run.clicked.connect(lambda: self.dryRunRequested.emit())
        row.addWidget(self._dry_run)

        self._execute = PrimaryPushButton("开始整理", self)
        self._execute.clicked.connect(lambda: self.executeRequested.emit())
        self._execute.setEnabled(False)
        row.addWidget(self._execute)
        return row

    # -- 对外 -------------------------------------------------------------

    def show_result(self, result: PlanResult) -> None:
        plan = result.plan
        self._plan = plan
        stats = plan.stats()

        self._tile_total.set_value(f"{stats.total_files:,}")
        self._tile_categories.set_value(f"{stats.category_count:,}")
        self._tile_pending.set_value(f"{stats.pending_move:,}")
        self._tile_conflicts.set_value(
            f"{stats.conflict_count:,}", warn=stats.conflict_count > 0
        )
        self._tile_unclassified.set_value(
            f"{stats.unclassified_count:,}", warn=stats.unclassified_count > 0
        )

        self.model.set_plan(plan)
        self.category_list.set_plan(plan)
        self.detail.clear()
        self.tree.expandToDepth(0)
        self._stack.setCurrentIndex(0)  # 显示主内容

        self.show_override_report(result.overrides)

        if result.space.ok:
            self._space_warning.setVisible(False)
            self._execute.setEnabled(stats.pending_move > 0)
        else:
            self._space_warning.setText(result.space.message)
            self._space_warning.setVisible(True)
            # 需求 9.10：空间不足整体阻止执行
            self._execute.setEnabled(False)

    def show_empty(self, title: str = "", hint: str = "") -> None:
        """显示空态。需求 17.9。"""
        if title:
            self._empty.set_text(title, hint)
        self._stack.setCurrentIndex(1)
        self._execute.setEnabled(False)

    def show_error(self, detail: str) -> None:
        """显示错误态。需求 17.11。"""
        self._error.set_error("方案生成失败", detail)
        self._stack.setCurrentIndex(2)
        self._execute.setEnabled(False)

    def show_override_report(self, report: ApplyReport) -> None:
        """需求 19.7、19.8。"""
        if report.kept or report.dropped_paths:
            self._overrides_label.setText(report.summary())
        else:
            self._overrides_label.setText("")
        self._clear_overrides.setEnabled(report.kept > 0)

    def set_ai_available(self, available: bool, enabled: bool = False) -> None:
        """需求 6.2、6.3。未配置 Provider 时开关置灰。"""
        self._ai_switch.setEnabled(available)
        self._ai_hint.setText("" if available else "未配置模型服务")
        self._set_switch_checked(self._ai_switch, enabled and available)

    def conflict_policy(self) -> ConflictPolicy:
        return _POLICY_LABELS[self._policy.currentIndex()][1]

    def cleanup_enabled(self) -> bool:
        return self._is_switch_checked(self._cleanup)

    def set_busy(self, busy: bool) -> None:
        for widget in (self._dry_run, self._execute, self._policy, self._back):
            widget.setEnabled(not busy)
        if not busy and self._plan is not None:
            self._execute.setEnabled(self._plan.stats().pending_move > 0)

    # -- 交互 -------------------------------------------------------------

    def _on_mode_changed(self, checked: bool) -> None:
        self.model.set_mode(ViewMode.AFTER if checked else ViewMode.BEFORE)
        self.tree.expandToDepth(0)

    def _on_search(self, text: str) -> None:
        self.proxy.set_query(text)

    def _on_filter_changed(self) -> None:
        self.proxy.set_only_conflicts(self._only_conflicts.isChecked())
        self.proxy.set_only_unclassified(self._only_unclassified.isChecked())

    def _on_current_changed(self, current: object) -> None:
        source = self.proxy.mapToSource(current)  # type: ignore[arg-type]
        item = self.model.item_at(source)
        if item is None:
            self.detail.clear()
        else:
            self.detail.show_item(item)

    def _on_model_data_changed(self, top_left: object, _bottom: object, roles: object) -> None:
        if Qt.ItemDataRole.CheckStateRole not in (roles or []):
            return
        item = self.model.item_at(top_left)  # type: ignore[arg-type]
        if item is not None:
            self.itemIncludedChanged.emit(item.entry.path, item.included)

    def _on_policy_changed(self, index: int) -> None:
        policy = _POLICY_LABELS[index][1]
        if policy is ConflictPolicy.OVERWRITE and not confirm(
            self,
            "确认使用覆盖策略？",
            "同名时目标文件会被移入回收站再被覆盖。虽然可以撤销，但这是三种策略里"
            "唯一会动到已有文件的一种。\n\n确认后还需要在开始整理时再确认一次。",
            ok_text="我明白，使用覆盖",
        ):
            # 需求 9.6：用户不确认就回退到默认策略
            self._policy.setCurrentIndex(0)
            return
        self.conflictPolicyChanged.emit(policy)

    def _on_cleanup_changed(self, checked: bool) -> None:
        """需求 20.3、20.4：false → true 必须弹警告。"""
        if not checked:
            self._cleanup_warned_off = True
            self.cleanupToggled.emit(False)
            return

        accepted = confirm(
            self,
            CLEANUP_SWITCH_LABEL,
            "开启后：\n\n"
            "· 只删除本次整理中被移空的子文件夹，不碰其他目录\n"
            "· 原有文件夹结构会因此改变\n"
            "· 该删除可通过撤销恢复\n\n"
            "执行前的确认框里会列出将被删除的完整清单。",
            ok_text="开启清理",
        )
        if not accepted:
            self._set_switch_checked(self._cleanup, False)
            return
        self.cleanupToggled.emit(True)

    def _on_clear_overrides(self) -> None:
        """需求 19.9、19.10。"""
        if confirm(
            self,
            "清除全部手工调整",
            "所有改名、换色、合并、排除与拖拽调整都会被清掉，方案回到规则算出的样子。"
            "此操作不可撤销。",
            ok_text="清除",
        ):
            self.clearOverridesRequested.emit()

    # -- 开关兼容 ---------------------------------------------------------

    @staticmethod
    def _is_switch_checked(switch: QWidget) -> bool:
        if hasattr(switch, "isChecked"):
            return bool(switch.isChecked())  # type: ignore[attr-defined]
        return False

    def _set_switch_checked(self, switch: QWidget, value: bool) -> None:
        if hasattr(switch, "setChecked"):
            switch.blockSignals(True)
            switch.setChecked(value)  # type: ignore[attr-defined]
            switch.blockSignals(False)


__all__ = ["PreviewPage"]
