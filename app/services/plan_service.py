"""PlanService：把 Planner 放进 QThread，并持有会话态的 OverrideSet。

本类是 UI 编辑动作与 ``Planner`` 之间的**唯一**通道。override 必须由一个地方持有：
它是规划链的独立输入，散落到各个页面各存一份的话，「切换策略后手工调整还在不在」
就取决于哪个页面先刷新，那是没法保证的（需求 19.3）。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal

from app.core.models import (
    ActionKind,
    ClassifyOptions,
    ConflictPolicy,
    FileEntry,
    OverrideSet,
    ScanSelection,
    Strategy,
)
from app.core.planner import PlanResult, Planner
from app.core.rules import RuleEngine
from app.core.safety import SafetyGuard
from app.services.base import WorkerService


class PlanService(WorkerService):
    """方案生成与重算。"""

    #: (保留的手工调整条数, 被丢弃的路径列表)。需求 19.7、19.8
    overridesKept = Signal(int, list)
    #: 跨卷空间不足，整体阻止执行。需求 9.10
    spaceInsufficient = Signal(str)

    def __init__(
        self,
        engine: RuleEngine | None = None,
        guard: SafetyGuard | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._guard = SafetyGuard() if guard is None else guard
        self._planner = Planner(engine=engine, guard=self._guard)
        self._overrides = OverrideSet()
        self._last: PlanResult | None = None

    # -- 只读访问 ---------------------------------------------------------

    @property
    def overrides(self) -> OverrideSet:
        return self._overrides

    @property
    def last_result(self) -> PlanResult | None:
        return self._last

    def set_engine(self, engine: RuleEngine) -> None:
        """规则库被修改后换引擎。下一次重算即生效（需求 4.8）。"""
        self._planner = Planner(engine=engine, guard=self._guard)

    # -- override -------------------------------------------------------

    def clear_overrides(self) -> None:
        """清除全部手工调整。需求 19.9、19.10 要求由 UI 二次确认后才调用。"""
        self._overrides.clear()

    # -- 重算 -----------------------------------------------------------

    def rebuild(
        self,
        entries: list[FileEntry],
        *,
        root: Path,
        strategy: Strategy,
        options: ClassifyOptions,
        conflict_policy: ConflictPolicy = ConflictPolicy.AUTO_RENAME,
        default_action: ActionKind = ActionKind.MOVE,
        selection: ScanSelection | None = None,
    ) -> bool:
        """重算方案。override 作为独立输入传入，因此重算不会冲掉手工调整。

        需求 19.3 列出的六个触发场景（切换 AI 开关 / 策略 / 规则库 / 重新扫描 /
        扫描范围 / 冲突策略）全部走这一个入口，保证行为一致。
        """
        overrides = self._overrides
        planner = self._planner
        snapshot = list(entries)

        def job(_cancel: object, _on_progress: object) -> PlanResult:
            return planner.build(
                snapshot,
                root=root,
                strategy=strategy,
                options=options,
                conflict_policy=conflict_policy,
                default_action=default_action,
                overrides=overrides,
                selection=selection,
            )

        return self._launch(job)

    def _transform(self, result: object) -> object:
        if isinstance(result, PlanResult):
            self._last = result
            report = result.overrides
            # 失效的 override 已在 resolve 阶段被识别，这里同步清掉，
            # 否则它们会在每次重算时反复出现在「已丢弃」提示里
            for stale in report.dropped_paths:
                self._overrides.items.pop(stale, None)
            self.overridesKept.emit(report.kept, list(report.dropped_paths))
            if not result.space.ok:
                self.spaceInsufficient.emit(result.space.message)
        return result
