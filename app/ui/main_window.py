"""主窗口：侧边导航 + 整理流程编排。需求 17.1、17.2。

本模块是 UI 的编排层：把页面信号接到 ``ScanService``，把 service 的信号接回页面。
业务判定一律不在这里——目录能不能整理由 ``SafetyGuard`` 说，扫到什么由
``ScanSession`` 说。

四个导航入口对应需求 17.1；整理主流程的四步（选择目录 / 扫描分析 / 方案预览 /
执行结果）对应需求 17.2，后两步在 M3、M4 落地，此处先放占位。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QStackedWidget, QVBoxLayout, QWidget

from app.config.settings import Settings, SettingsManager, default_app_dir
from app.core.history import HistoryManager
from app.core.models import (
    CategoryOverride,
    ConflictPolicy,
    ItemOverride,
    ScanOptions,
)
from app.core.planner import PlanResult
from app.core.progress import ProgressSnapshot
from app.core.rules import RuleEngine
from app.core.safety import AdmissionResult, SafetyGuard
from app.core.scanner import ScanDelta, ScanResult
from app.services.execute_service import ExecuteService, UndoService
from app.services.plan_service import PlanService
from app.services.scan_service import ScanService
from app.ui.pages.history_page import HistoryPage
from app.ui.pages.preview_page import PreviewPage
from app.ui.pages.result_page import ResultPage
from app.ui.pages.rules_page import RulesPage
from app.ui.pages.scan_page import ScanPage
from app.ui.pages.select_page import SelectPage
from app.ui.pages.settings_page import SettingsPage
from app.ui.theme import (
    SPACE_XL,
    FLUENT_AVAILABLE,
    CaptionLabel,
    FluentWindow,
    TitleLabel,
    app_stylesheet,
    apply_system_theme,
    choose_option,
    confirm,
    countdown_to_start,
    toast_error,
    toast_info,
)
from app.ui.theme.components import FIF, NavigationItemPosition

logger = logging.getLogger(__name__)

STEP_SELECT = 0
STEP_SCAN = 1
STEP_PREVIEW = 2
STEP_RESULT = 3


class _Placeholder(QWidget):
    """尚未实现的页面。明确写出「在哪个阶段落地」，避免看起来像坏了。"""

    def __init__(self, title: str, note: str, object_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE_XL, SPACE_XL, SPACE_XL, SPACE_XL)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(TitleLabel(title, self), alignment=Qt.AlignmentFlag.AlignHCenter)
        hint = CaptionLabel(note, self)
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)


class SortFlowPage(QWidget):
    """整理主流程：四步共用一个 QStackedWidget。"""

    #: 历史记录发生变化（执行完 / 撤销完），让历史页刷新
    historyChanged = Signal()

    def __init__(
        self,
        service: ScanService,
        settings: Settings,
        plan_service: PlanService | None = None,
        history_dir: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("sortFlowPage")
        self._service = service
        self._plan_service = plan_service or PlanService(guard=service.guard, parent=self)
        self._settings = settings
        self._pending_toggle: tuple[Path, bool] | None = None
        self._conflict_policy = settings.conflict.policy
        self._last_run_id: str | None = None
        #: 需求 11.4 的取消窗口长度。做成实例属性只为让测试能设成 0——真实使用中
        #: 它恒为 3，而测试不该为了等 3 秒而慢下来
        self._cancel_window_seconds = 3

        history_root = Path(history_dir) if history_dir else default_app_dir() / "history"
        history_root.mkdir(parents=True, exist_ok=True)
        self._history = HistoryManager(history_root)
        self._execute_service = ExecuteService(history_root, parent=self)
        self._undo_service = UndoService(history_root, parent=self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._stack = QStackedWidget(self)
        layout.addWidget(self._stack)

        self.select_page = SelectPage(self)
        self.scan_page = ScanPage(self)
        self.preview_page = PreviewPage(self)
        self.result_page = ResultPage(self)
        self._stack.addWidget(self.select_page)
        self._stack.addWidget(self.scan_page)
        self._stack.addWidget(self.preview_page)
        self._stack.addWidget(self.result_page)

        self.select_page.set_recent(list(settings.ui.recent_roots))
        self.preview_page.set_ai_available(False)

        self._wire()
        self._wire_preview()
        self._wire_execution()

    @property
    def history(self) -> HistoryManager:
        return self._history

    @property
    def execute_service(self) -> ExecuteService:
        return self._execute_service

    @property
    def undo_service(self) -> UndoService:
        return self._undo_service

    def _wire_execution(self) -> None:
        page = self.result_page
        page.backRequested.connect(lambda: self._stack.setCurrentIndex(STEP_PREVIEW))
        page.stopRequested.connect(self._execute_service.cancel)
        page.undoRequested.connect(self._on_undo_requested)
        page.redoRequested.connect(self._on_redo_requested)
        page.openFolderRequested.connect(self._open_root)

        self._execute_service.progressChanged.connect(page.show_progress)
        self._execute_service.finished.connect(self._on_execute_finished)
        self._execute_service.failed.connect(
            lambda detail: toast_error(self, "执行失败", detail)
        )
        self._execute_service.busyChanged.connect(page.set_busy)

        self._undo_service.progressChanged.connect(page.show_progress)
        self._undo_service.finished.connect(self._on_undo_finished)
        self._undo_service.failed.connect(
            lambda detail: toast_error(self, "撤销失败", detail)
        )
        self._undo_service.busyChanged.connect(page.set_busy)

    def undo_run(self, run_id: str) -> None:
        """供历史页调用：撤销指定 run。需求 15.2。"""
        self._last_run_id = run_id
        self._stack.setCurrentIndex(STEP_RESULT)
        self.result_page.show_running(0)
        self._undo_service.start_undo(run_id)

    def resume_run(self, run_id: str, pending_sources: list[str]) -> bool:
        """供崩溃恢复对话框调用：把未收尾的 run 接着跑完。需求 13.8。

        接着写同一份 journal、沿用 manifest 里的方案与选项——那次整理在用户眼里是
        一次操作，撤销时也该整体回滚。
        """
        self._last_run_id = run_id
        self._stack.setCurrentIndex(STEP_RESULT)
        self.result_page.show_running(len(pending_sources))
        started = self._execute_service.start_resume(
            run_id, pending_sources, selection=self._selection()
        )
        if not started:
            toast_info(
                self,
                "没有可恢复的条目",
                "停在中间的文件都已不在原处，请用「全部撤销」或到历史记录里处理。",
            )
        return started

    # -- 接线 -------------------------------------------------------------

    def _wire(self) -> None:
        self.select_page.scanRequested.connect(self._start_scan)

        self.scan_page.selectionToggled.connect(self._toggle_subfolder)
        self.scan_page.expandRequested.connect(self._expand)
        self.scan_page.retryRequested.connect(self._retry)
        self.scan_page.cancelRequested.connect(self._service.cancel)
        self.scan_page.proceedRequested.connect(self._build_plan)

        self._service.rootRejected.connect(self._on_root_rejected)
        self._service.progressChanged.connect(self._on_progress)
        self._service.subfoldersReady.connect(lambda _infos: None)
        self._service.deltaReady.connect(self._on_delta)
        self._service.finished.connect(self._on_finished)
        self._service.failed.connect(self._on_failed)
        self._service.cancelledSignal.connect(self._on_cancelled)
        self._service.busyChanged.connect(self._on_busy)

    def _wire_preview(self) -> None:
        page = self.preview_page
        page.backRequested.connect(lambda: self._stack.setCurrentIndex(STEP_SCAN))
        page.conflictPolicyChanged.connect(self._set_conflict_policy)
        page.cleanupToggled.connect(self._set_cleanup)
        page.clearOverridesRequested.connect(self._clear_overrides)
        page.dryRunRequested.connect(self._dry_run)
        page.executeRequested.connect(self._execute)

        page.renameCategory.connect(self._rename_category)
        page.recolorCategory.connect(self._recolor_category)
        page.mergeCategory.connect(self._merge_category)
        page.createCategory.connect(self._create_category)
        page.deleteCategory.connect(self._delete_category)
        page.itemIncludedChanged.connect(self._set_item_included)
        page.itemsCategoryChanged.connect(self._set_items_category)

        self._plan_service.finished.connect(self._on_plan_ready)
        self._plan_service.failed.connect(self._on_plan_failed)
        self._plan_service.busyChanged.connect(page.set_busy)
        self._plan_service.spaceInsufficient.connect(
            lambda message: toast_error(self, "空间不足", message)
        )

    # -- 方案 -------------------------------------------------------------

    def _build_plan(self) -> None:
        session = self._service.session
        root = self.select_page.root
        if session is None or root is None:
            return
        entries = session.entries()
        if not entries:
            toast_info(self, "没有可整理的文件", "当前扫描范围内没有散落文件。")
            return

        self._stack.setCurrentIndex(STEP_PREVIEW)
        settings = self._settings
        self._plan_service.rebuild(
            entries,
            root=session.root,
            strategy=settings.classify.strategy,
            options=SettingsManager.to_classify_options(settings),
            conflict_policy=self._conflict_policy,
            default_action=settings.conflict.default_action,
            selection=session.selection(),
        )

    def _rebuild_plan(self) -> None:
        """任何改动方案的操作都走这一个入口，保证 override 一致地被叠加。"""
        if self._service.session is not None:
            self._build_plan()

    def _on_plan_ready(self, payload: object) -> None:
        if isinstance(payload, PlanResult):
            self.preview_page.show_result(payload)

    def _on_plan_failed(self, detail: str) -> None:
        toast_error(self, "方案生成失败", detail)

    # -- 方案编辑（全部记入 override）--------------------------------------

    def _overrides(self) -> object:
        return self._plan_service.overrides

    def _rename_category(self, old: tuple[str, ...], new: tuple[str, ...]) -> None:
        store = self._plan_service.overrides.categories
        existing = store.get(old, CategoryOverride())
        existing.renamed_to = new
        store[old] = existing
        self._rebuild_plan()

    def _recolor_category(self, parts: tuple[str, ...], color: str) -> None:
        store = self._plan_service.overrides.categories
        existing = store.get(parts, CategoryOverride())
        existing.color = color
        store[parts] = existing
        self._rebuild_plan()

    def _merge_category(self, source: tuple[str, ...], target: tuple[str, ...]) -> None:
        store = self._plan_service.overrides.categories
        existing = store.get(source, CategoryOverride())
        existing.merged_into = target
        store[source] = existing
        self._rebuild_plan()

    def _create_category(self, parts: tuple[str, ...]) -> None:
        store = self._plan_service.overrides.categories
        existing = store.get(parts, CategoryOverride())
        existing.created = True
        store[parts] = existing
        toast_info(
            self,
            "类目已创建",
            "把文件拖到这个类目里，或在文件详情里改归属，它才会出现在方案中。",
        )
        self._rebuild_plan()

    def _delete_category(self, parts: tuple[str, ...]) -> None:
        store = self._plan_service.overrides.categories
        existing = store.get(parts, CategoryOverride())
        existing.deleted = True
        store[parts] = existing
        self._rebuild_plan()

    def _set_item_included(self, path: Path, included: bool) -> None:
        store = self._plan_service.overrides.items
        key = str(path)
        existing = store.get(key, ItemOverride())
        existing.included = included
        store[key] = existing
        # 勾选状态已经在模型里改过了，不必整体重算——重算会把树折叠回去，
        # 用户连续勾掉几十项时那很难用。override 已记下，下次重算自然生效。
        self.preview_page.show_override_report(
            self._plan_service.last_result.overrides
            if self._plan_service.last_result
            else self._empty_report()
        )

    def _set_items_category(
        self, paths: list[Path], target: tuple[str, ...]
    ) -> None:
        """拖拽改类目（需求 10.11）。

        与勾选不同，这里**必须**重算：条目换了类目，target 与冲突都得重新
        预检，只有 Planner 能算（需求 10.11 要求同时更新 category_id 与 target）。
        """
        store = self._plan_service.overrides.items
        for path in paths:
            key = str(path)
            existing = store.get(key, ItemOverride())
            existing.category_path = target
            store[key] = existing
        self._rebuild_plan()

    @staticmethod
    def _empty_report() -> object:
        from app.core.overrides import ApplyReport

        return ApplyReport()

    def _clear_overrides(self) -> None:
        self._plan_service.clear_overrides()
        self._rebuild_plan()

    def _set_conflict_policy(self, policy: ConflictPolicy) -> None:
        self._conflict_policy = policy
        self._settings.conflict.policy = policy
        self._rebuild_plan()

    def _set_cleanup(self, enabled: bool) -> None:
        self._settings.cleanup.remove_empty_dirs = enabled

    # -- 执行与撤销 -------------------------------------------------------

    def _current_plan(self) -> object | None:
        result = self._plan_service.last_result
        return result.plan if result else None

    def _exec_options(self, dry_run: bool):
        from app.config.settings import SettingsManager as SM

        options = SM.to_exec_options(self._settings, dry_run=dry_run)
        return options

    def _dry_run(self) -> None:
        plan = self._current_plan()
        if plan is None:
            return
        self.result_page.show_running(len(plan.all_items()), dry_run=True)
        self._stack.setCurrentIndex(STEP_RESULT)
        self._execute_service.start_execute(
            plan,
            self._exec_options(dry_run=True),
            selection=self._selection(),
            overrides=self._plan_service.overrides,
        )

    def _execute(self) -> None:
        plan = self._current_plan()
        if plan is None:
            return

        root = str(plan.root)
        # 需求 11.2：首次对某个根目录执行前，强制先跑一次模拟运行
        if root not in self._settings.ui.dry_run_completed_roots:
            toast_info(
                self,
                "先跑一次模拟运行",
                "这是第一次整理这个目录。模拟运行不会改动任何文件，看过结果再来执行。",
            )
            self._dry_run()
            return

        stats = plan.stats()
        folders = len({i.target.parent for i in plan.all_items() if i.included})
        # 需求 11.3：确认框必须写出具体数字与可撤销的说明
        if not confirm(
            self,
            "开始整理",
            f"将移动 {stats.pending_move} 个文件到 {folders} 个文件夹。\n\n"
            "此操作可完整撤销：完成后在结果页点「撤销本次整理」，或之后在"
            "「历史记录」里撤销。",
            ok_text="开始整理",
        ):
            return

        # 需求 11.4：确认之后还留 3 秒取消窗口。误点击的典型形态是手比脑子快，
        # 连点两下正好把确认框也点掉——这 3 秒是唯一能救回那种情况的地方。
        if not countdown_to_start(
            self,
            f"{stats.pending_move} 个文件 · {folders} 个文件夹 · {plan.root}",
            seconds=self._cancel_window_seconds,
        ):
            toast_info(self, "已取消", "一个文件都没有动。")
            return

        self.result_page.show_running(len(plan.all_items()))
        self._stack.setCurrentIndex(STEP_RESULT)
        self._execute_service.start_execute(
            plan,
            self._exec_options(dry_run=False),
            selection=self._selection(),
            overrides=self._plan_service.overrides,
        )

    def _selection(self):
        session = self._service.session
        return session.selection() if session else None

    def _on_execute_finished(self, payload: object) -> None:
        from app.core.models import ExecutionReport

        if not isinstance(payload, ExecutionReport):
            return

        if payload.dry_run:
            plan = self._current_plan()
            if plan is not None:
                root = str(plan.root)
                done = set(self._settings.ui.dry_run_completed_roots)
                done.add(root)
                self._settings.ui.dry_run_completed_roots = tuple(sorted(done))
            self.result_page.show_report(payload, undoable=False)
            return

        self._last_run_id = self._execute_service.last_run_id
        self.result_page.show_report(payload, undoable=bool(self._last_run_id))
        self.historyChanged.emit()

    def _on_undo_requested(self) -> None:
        if self._last_run_id:
            self._undo_service.start_undo(self._last_run_id)

    def _on_redo_requested(self) -> None:
        if self._last_run_id:
            self._undo_service.start_redo(self._last_run_id)

    def _on_undo_finished(self, payload: object) -> None:
        from app.core.undo import UndoReport

        if not isinstance(payload, UndoReport):
            return
        self.result_page.show_undo_report(payload)
        if self._last_run_id:
            self._history.mark_undone(self._last_run_id)
        self.historyChanged.emit()
        if payload.needs_attention:
            toast_info(
                self,
                f"{len(payload.needs_attention)} 项需人工确认",
                "这些文件在整理之后被改动过，没有被覆盖。详情见「需人工确认」标签。",
            )

    def _open_root(self) -> None:
        import subprocess

        plan = self._current_plan()
        if plan is None:
            return
        try:
            subprocess.Popen(["explorer", str(plan.root)])  # noqa: S603, S607
        except OSError as exc:
            toast_error(self, "无法打开目录", str(exc))

    # -- 流程 -------------------------------------------------------------

    def _start_scan(self, root: Path, options: ScanOptions) -> None:
        self.scan_page.set_root(root)
        self.scan_page.show_scanning()
        self._stack.setCurrentIndex(STEP_SCAN)
        if not self._service.start_scan(root, options):
            # 准入失败已由 rootRejected 处理，退回第一步
            self._stack.setCurrentIndex(STEP_SELECT)

    def _retry(self) -> None:
        root = self.select_page.root
        if root is not None:
            self._start_scan(root, self.select_page.options())

    def _toggle_subfolder(self, folder: Path, selected: bool) -> None:
        self._pending_toggle = (folder, selected)
        if not self._service.start_set_selected(folder, selected):
            # 上一个作业还在跑：把复选框弹回原状，避免界面与实际状态不一致
            self.scan_page.revert_checkbox(folder, not selected)
            self._pending_toggle = None

    def _expand(self, folder: Path) -> None:
        infos = self._service.expand(folder)
        self.scan_page.set_children(folder, infos)

    # -- service 回调 -----------------------------------------------------

    def _on_root_rejected(self, result: AdmissionResult) -> None:
        self.select_page.show_admission(result)
        self._stack.setCurrentIndex(STEP_SELECT)
        toast_error(self, "无法整理该目录", result.message)

    def _on_progress(self, snapshot: ProgressSnapshot) -> None:
        self.scan_page.show_progress(snapshot)

    def _on_finished(self, payload: object) -> None:
        if isinstance(payload, ScanResult):
            self._on_scan_done(payload)
        elif isinstance(payload, ScanDelta):
            self._refresh_counts()

    def _on_scan_done(self, result: ScanResult) -> None:
        if result.cancelled:
            self.scan_page.show_error("扫描已取消，没有产生任何结果。")
            return
        self.scan_page.show_result(result)
        self._remember_recent()
        if result.errors:
            toast_info(
                self,
                "部分条目未能读取",
                f"{len(result.errors)} 项因权限或 I/O 问题被跳过，其余已正常收集。",
            )

    def _on_delta(self, delta: ScanDelta) -> None:
        if delta.cancelled and self._pending_toggle is not None:
            folder, selected = self._pending_toggle
            self.scan_page.revert_checkbox(folder, not selected)
            toast_info(self, "已取消", "补扫被取消，勾选状态未改变。")
        self._pending_toggle = None

    def _on_failed(self, detail: str) -> None:
        self.scan_page.show_error(detail)
        toast_error(self, "出错了", detail)

    def _on_cancelled(self) -> None:
        self.scan_page.show_error("操作已取消。")

    def _on_busy(self, busy: bool) -> None:
        self.select_page.set_busy(busy)
        self.scan_page.set_busy(busy)

    # -- 辅助 -------------------------------------------------------------

    def _refresh_counts(self) -> None:
        session = self._service.session
        if session is None:
            return
        self.scan_page.update_counts(
            len(session.entries()), len(session.selection().selected)
        )

    def _remember_recent(self) -> None:
        root = self.select_page.root
        if root is None:
            return
        text = str(root)
        recent = [text, *(r for r in self._settings.ui.recent_roots if r != text)]
        self._settings.ui.recent_roots = tuple(recent[:6])
        self.select_page.set_recent(list(self._settings.ui.recent_roots))


def _icon(*names: str) -> object | None:
    """按顺序取第一个存在的 FluentIcon 成员。

    组件库不同版本的图标枚举有增删，写死名字会让「换个版本就启动不了」。
    """
    if FIF is None:
        return None
    for name in names:
        icon = getattr(FIF, name, None)
        if icon is not None:
            return icon
    return None


class MainWindow(FluentWindow):
    """应用主窗口。

    ``FluentWindow`` 在组件库缺失时回落成 ``QMainWindow``，两者导航 API 不同，
    因此 ``_build_navigation`` 里按 ``FLUENT_AVAILABLE`` 分支。
    """

    def __init__(self, settings_manager: SettingsManager | None = None) -> None:
        super().__init__()
        self._settings_manager = settings_manager or SettingsManager()
        self._settings = self._settings_manager.load()

        guard = SafetyGuard()
        self._scan_service = ScanService(guard=guard, parent=self)

        apply_system_theme()
        self.setStyleSheet(app_stylesheet())
        self.setWindowTitle("DocSorter 文档分类工具")
        # 默认高度低于常见 768px 屏幕减去任务栏后的可用空间，保证底部操作条
        # （「开始整理」）始终落在可视区域内、可以被点到。
        self.resize(1000, 600)
        # 允许窗口进一步缩小到更矮的屏幕，避免底部操作条被挤出可视区
        self.setMinimumSize(840, 480)

        engine, rule_errors = self._load_rules()
        self._plan_service = PlanService(engine=engine, guard=guard, parent=self)
        if rule_errors:
            logger.warning(
                "规则文件有 %d 处问题，已回落到内置默认规则", len(rule_errors)
            )

        self.sort_flow = SortFlowPage(
            self._scan_service,
            self._settings,
            self._plan_service,
            history_dir=self._settings_manager.history_dir,
            parent=self,
        )
        self.history_page = HistoryPage(self)
        self.history_page.refreshRequested.connect(self._refresh_history)
        self.history_page.undoRequested.connect(self.sort_flow.undo_run)
        self.sort_flow.historyChanged.connect(self._refresh_history)
        self.sort_flow.undo_service.busyChanged.connect(self.history_page.set_busy)
        self._refresh_history()
        self._apply_retention()
        self._check_unfinished()
        self.rules_page = RulesPage(self._settings_manager, self)
        self.rules_page.rulesSaved.connect(self._on_rules_saved)
        self.settings_page = SettingsPage(self._settings, self._settings_manager, self)
        self.settings_page.settingsChanged.connect(self._on_settings_changed)

        self._build_navigation()

    def _build_navigation(self) -> None:
        """侧边导航四入口。需求 17.1。"""
        if not FLUENT_AVAILABLE:  # pragma: no cover - 降级路径
            central = QStackedWidget(self)
            for page in (
                self.sort_flow,
                self.history_page,
                self.rules_page,
                self.settings_page,
            ):
                central.addWidget(page)
            self.setCentralWidget(central)
            return

        self.addSubInterface(
            self.sort_flow, _icon("BROOM", "FOLDER", "HOME"), "整理流程"
        )
        self.addSubInterface(
            self.history_page, _icon("HISTORY", "DATE_TIME", "CALENDAR"), "历史记录"
        )
        self.addSubInterface(
            self.rules_page, _icon("TILES", "DICTIONARY", "LIBRARY"), "规则管理"
        )
        self.addSubInterface(
            self.settings_page,
            _icon("SETTING", "SETTINGS"),
            "设置",
            position=NavigationItemPosition.BOTTOM,
        )

    def _on_rules_saved(self) -> None:
        """规则保存后重新加载引擎。需求 4.8。"""
        from app.core.rules import RuleEngine, builtin_rules
        rules_text = (self._settings_manager.base_dir / "rules.yaml").read_text(encoding="utf-8")
        engine, errors = RuleEngine.from_text(rules_text)
        if not errors:
            self.sort_flow.plan_service.set_engine(engine)

    def _on_settings_changed(self) -> None:
        """设置变更后更新内部状态。"""
        self._settings = self._settings_manager.load()

    def _refresh_history(self) -> None:
        self.history_page.set_runs(self.sort_flow.history.list_runs())

    def _apply_retention(self) -> None:
        """需求 15.4：启动时跑一次保留策略。"""
        removed = self.sort_flow.history.apply_retention(
            max_runs=self._settings.history.max_runs,
            max_days=self._settings.history.max_days,
        )
        if removed:
            logger.info("按保留策略清理了 %d 条历史记录", len(removed))
            self._refresh_history()

    def _check_unfinished(self) -> None:
        """需求 13.7、13.8：启动时检测未收尾的 run 并让用户选择如何处理。"""
        unfinished = self.sort_flow.history.find_unfinished()
        if not unfinished:
            return

        latest = unfinished[0]
        resume = self.sort_flow.undo_service.manager.resume(latest.run_id)
        pending = [
            *resume.at_source,
            *resume.at_target,
            *resume.both,
            *resume.missing,
        ]

        # 需求 13.7 点名要三个出口，confirm() 只有两个按钮。硬塞第三个选项只能靠
        # 「取消 = 第三种意思」这种暗示，而这是一次可能动上千文件的决定。
        choice = choose_option(
            self,
            "上次整理没有正常结束",
            f"时间：{latest.meta.started_at}\n"
            f"目录：{latest.meta.root}\n"
            f"已完成：{latest.moved} 个文件\n"
            f"停在中间的：{len(pending)} 个\n\n"
            f"「恢复执行」把仍留在原处的 {len(resume.at_source)} 个文件接着搬完；\n"
            "「全部撤销」回到整理前的样子，停在中间的条目会列进「需人工确认」；\n"
            "「忽略」保持现状，之后仍可在历史记录里撤销。",
            ["恢复执行", "全部撤销", "忽略"],
        )
        if choice == 0:
            self.sort_flow.resume_run(latest.run_id, pending)
        elif choice == 1:
            self.sort_flow.undo_run(latest.run_id)

    def _load_rules(self) -> tuple[RuleEngine, list]:
        """加载用户规则文件，失败则回落到内置默认规则（需求 4.3）。"""
        path = self._settings_manager.rules_path
        if not path.exists():
            return RuleEngine(), []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("读取规则文件失败: %s", exc)
            return RuleEngine(), []
        return RuleEngine.from_text(text)

    def closeEvent(self, event: object) -> None:  # noqa: N802
        """关窗前收尾后台作业并落盘配置。"""
        for service in (
            self._scan_service,
            self._plan_service,
            self.sort_flow.execute_service,
            self.sort_flow.undo_service,
        ):
            service.cancel()
            service.wait(3000)
        try:
            self._settings_manager.save(self._settings)
        except OSError as exc:
            logger.warning("保存配置失败: %s", exc)
        super().closeEvent(event)  # type: ignore[misc]
