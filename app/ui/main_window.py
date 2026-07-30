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

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QStackedWidget, QVBoxLayout, QWidget

from app.config.settings import Settings, SettingsManager
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
from app.services.plan_service import PlanService
from app.services.scan_service import ScanService
from app.ui.pages.preview_page import PreviewPage
from app.ui.pages.scan_page import ScanPage
from app.ui.pages.select_page import SelectPage
from app.ui.theme import (
    SPACE_XL,
    FLUENT_AVAILABLE,
    CaptionLabel,
    FluentWindow,
    TitleLabel,
    app_stylesheet,
    apply_system_theme,
    toast_error,
    toast_info,
)
from app.ui.theme.components import FIF, NavigationItemPosition

logger = logging.getLogger(__name__)

STEP_SELECT = 0
STEP_SCAN = 1
STEP_PREVIEW = 2


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

    def __init__(
        self,
        service: ScanService,
        settings: Settings,
        plan_service: PlanService | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("sortFlowPage")
        self._service = service
        self._plan_service = plan_service or PlanService(guard=service.guard, parent=self)
        self._settings = settings
        self._pending_toggle: tuple[Path, bool] | None = None
        self._conflict_policy = settings.conflict.policy

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._stack = QStackedWidget(self)
        layout.addWidget(self._stack)

        self.select_page = SelectPage(self)
        self.scan_page = ScanPage(self)
        self.preview_page = PreviewPage(self)
        self._stack.addWidget(self.select_page)
        self._stack.addWidget(self.scan_page)
        self._stack.addWidget(self.preview_page)

        self.select_page.set_recent(list(settings.ui.recent_roots))
        self.preview_page.set_ai_available(False)

        self._wire()
        self._wire_preview()

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

    def _dry_run(self) -> None:
        toast_info(
            self,
            "模拟运行将在 M4 阶段接入",
            "执行器与撤销机制尚未实现。当前方案已完整生成，可以逐条核对。",
        )

    def _execute(self) -> None:
        toast_info(
            self,
            "执行将在 M4 阶段接入",
            "执行器、操作日志与撤销必须一起落地——没有撤销的执行不该被允许。",
        )

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
        self.resize(1180, 760)

        engine, rule_errors = self._load_rules()
        self._plan_service = PlanService(engine=engine, guard=guard, parent=self)
        if rule_errors:
            logger.warning(
                "规则文件有 %d 处问题，已回落到内置默认规则", len(rule_errors)
            )

        self.sort_flow = SortFlowPage(
            self._scan_service, self._settings, self._plan_service, self
        )
        self.history_page = _Placeholder(
            "历史记录",
            "每次整理的时间轴与逐条撤销将在 M4 阶段落地。",
            "historyPage",
            self,
        )
        self.rules_page = _Placeholder(
            "规则管理",
            "关键词与扩展名规则的编辑将在 M5 阶段落地。",
            "rulesPage",
            self,
        )
        self.settings_page = _Placeholder(
            "设置",
            "AI、隐私档、保留策略等选项将在 M5 阶段落地。",
            "settingsPage",
            self,
        )

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
        for service in (self._scan_service, self._plan_service):
            service.cancel()
            service.wait(3000)
        try:
            self._settings_manager.save(self._settings)
        except OSError as exc:
            logger.warning("保存配置失败: %s", exc)
        super().closeEvent(event)  # type: ignore[misc]
