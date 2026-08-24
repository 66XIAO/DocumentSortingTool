"""PlanService：把 Planner 放进 QThread，并持有会话态的 OverrideSet。

本类是 UI 编辑动作与 ``Planner`` 之间的**唯一**通道。override 必须由一个地方持有：
它是规划链的独立输入，散落到各个页面各存一份的话，「切换策略后手工调整还在不在」
就取决于哪个页面先刷新，那是没法保证的（需求 19.3）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from app.core.classifiers.base import Suggestion
from app.core.inspector import extract_for_entries
from app.core.llm.cache import LLMCache
from app.core.llm.runner import (
    LLMRunner,
    LLMRunResult,
    estimate_requests,
    make_provider_for,
    provider_configured,
)
from app.core.models import (
    ActionKind,
    AIOptions,
    ClassifyOptions,
    ConflictPolicy,
    FileEntry,
    OverrideSet,
    ScanSelection,
    Strategy,
)
from app.core.planner import PlanResult, Planner
from app.core.progress import CancelToken
from app.core.rules import RuleEngine
from app.core.safety import SafetyGuard
from app.services.base import WorkerService

logger = logging.getLogger(__name__)


class PlanService(WorkerService):
    """方案生成与重算。"""

    #: (保留的手工调整条数, 被丢弃的路径列表)。需求 19.7、19.8
    overridesKept = Signal(int, list)
    #: 跨卷空间不足，整体阻止执行。需求 9.10
    spaceInsufficient = Signal(str)
    #: LLM 调用失败并已回落到规则分类，参数是给用户看的说明。需求 6.5
    aiFallback = Signal(str)
    #: (待处理文件数, 预估请求次数)。需求 8.6
    aiEstimate = Signal(int, int)

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
        self._ai = AIOptions()
        self._api_key = ""
        self._cache_path: Path | None = None
        self._cache: LLMCache | None = None
        self._last_ai: LLMRunResult | None = None

    # -- 只读访问 ---------------------------------------------------------

    @property
    def overrides(self) -> OverrideSet:
        return self._overrides

    @property
    def last_result(self) -> PlanResult | None:
        return self._last

    @property
    def last_ai_result(self) -> LLMRunResult | None:
        return self._last_ai

    def set_engine(self, engine: RuleEngine) -> None:
        """规则库被修改后换引擎。下一次重算即生效（需求 4.8）。"""
        self._planner = Planner(engine=engine, guard=self._guard)

    # -- AI 配置 ---------------------------------------------------------

    def configure_ai(
        self,
        options: AIOptions,
        api_key: str = "",
        cache_path: Path | None = None,
    ) -> None:
        """注入 AI 配置。

        api_key 由调用方从 keyring 取出后传入，不写进 ``AIOptions``，也不落盘
        （需求 7.2、7.3）。缓存路径变了就丢掉旧连接，下次重算按新路径重建。
        """
        self._ai = options
        self._api_key = api_key
        if cache_path != self._cache_path:
            self._cache_path = cache_path
            self._cache = None

    @property
    def ai_options(self) -> AIOptions:
        return self._ai

    def ai_available(self) -> bool:
        """是否配置出了可用 Provider。供 UI 决定开关是否置灰（需求 6.3）。"""
        return provider_configured(self._ai, self._api_key)

    def _llm_cache(self) -> LLMCache | None:
        if self._cache_path is None:
            return None
        if self._cache is None:
            try:
                self._cache = LLMCache(self._cache_path)
            except Exception as exc:  # noqa: BLE001 - 缓存不可用不该阻断分类
                logger.warning("LLM 缓存不可用，本次不使用缓存: %s", exc)
                self._cache_path = None
                return None
        return self._cache

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
        ai = self._ai
        api_key = self._api_key
        smart = strategy is Strategy.SMART
        use_ai = smart and ai.enabled and provider_configured(ai, api_key)
        cache = self._llm_cache() if use_ai else None

        # 需求 8.6：方案还没生成时就要能看到「多少文件、大约几次请求」
        if use_ai:
            self.aiEstimate.emit(
                len(snapshot), estimate_requests(len(snapshot), ai.batch_size)
            )

        def job(cancel: CancelToken, _on_progress: object) -> PlanResult:
            # 正文提取放在**方案作业**里而不是扫描作业里，是为了让「切到智能策略」
            # 不必重新扫一遍目录：提取结果直接写回共享的 FileEntry 上，已经有
            # text_head 的条目会被跳过，因此对同一批文件最多只做一次。
            if smart:
                _extract_content(snapshot, cancel)

            llm: dict[str, Suggestion] = {}
            ai_result: LLMRunResult | None = None
            if use_ai and not cancel.cancelled:
                runner = LLMRunner(
                    provider=make_provider_for(ai, api_key),
                    options=ai,
                    cache=cache,
                )
                ai_result = runner.run(snapshot, root, cancel=cancel)
                llm = ai_result.suggestions

            result = planner.build(
                snapshot,
                root=root,
                strategy=strategy,
                options=options,
                conflict_policy=conflict_policy,
                default_action=default_action,
                overrides=overrides,
                selection=selection,
                llm_suggestions=llm,
                ai_enabled=use_ai,
            )
            # 附在结果上带回主线程：_transform 在主线程跑，不能去读工作线程的局部量
            setattr(result, "ai_result", ai_result)
            return result

        return self._launch(job)

    def _transform(self, result: object) -> object:
        if isinstance(result, PlanResult):
            self._last = result
            ai_result = getattr(result, "ai_result", None)
            self._last_ai = ai_result if isinstance(ai_result, LLMRunResult) else None
            # 需求 6.5：失败只提示并回落，方案照常给出，不当成 failed
            if self._last_ai is not None and self._last_ai.failure:
                self.aiFallback.emit(self._last_ai.failure)
            report = result.overrides
            # 失效的 override 已在 resolve 阶段被识别，这里同步清掉，
            # 否则它们会在每次重算时反复出现在「已丢弃」提示里
            for stale in report.dropped_paths:
                self._overrides.items.pop(stale, None)
            self.overridesKept.emit(report.kept, list(report.dropped_paths))
            if not result.space.ok:
                self.spaceInsufficient.emit(result.space.message)
        return result


def _extract_content(entries: list[FileEntry], cancel: CancelToken) -> None:
    """为还没有正文的条目提取 text_head，结果写回 FileEntry。需求 5.1-5.4。

    只挑 ``text_head`` 与 ``error`` 都为空的条目：提取失败过的不重试，否则每次
    重算都要在同一个坏文件上再耗一遍。
    """
    pending = [e for e in entries if not e.text_head and not e.error]
    if not pending:
        return

    by_path = {str(e.path): e for e in pending}
    for extracted in extract_for_entries(pending, cancel=cancel):
        entry = by_path.get(str(extracted.path))
        if entry is None:
            continue
        if extracted.text_head:
            entry.text_head = extracted.text_head
        if extracted.error:
            # 需求 5.3：单个文件读不出来不影响其余，原因记在条目上
            entry.error = extracted.error
