"""Planner：从 FileEntry 集合产出 SortPlan。需求 9。

五步流水线，顺序即正确性：

    1) 管线分类        每个 entry 得到一条 Suggestion
    2) 小类目合并      文件数低于阈值的类目并入「其他」（需求 3.12）
    3) override 叠加   用户手工调整覆盖分类器结果（M3 接入，此处留钩子）
    4) 类目清洗 + target 构造
    5) 冲突预检        五态判定（需求 9.3）

第 2 步必须在第 3 步**之前**：否则用户手工建的小类目会被自动合并掉，而用户意图
的优先级高于自动规则。

跨卷空间校验（需求 9.10）按目标卷分组累加，与 1.1 倍余量比较后整体阻止执行——
执行到一半才发现空间不足，留下的是半整理状态。
"""

from __future__ import annotations

import shutil
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.core import fsops
from app.core.classifiers.base import (
    OTHER,
    UNCLASSIFIED,
    SOURCE_DATE,
    STRATEGIES_WITH_DATE_SUFFIX,
    Classifier,
    ClassifierPipeline,
    ClassifyContext,
    Suggestion,
    build_pipeline,
)
from app.core.classifiers.by_date import DateClassifier, date_parts
from app.core.classifiers.by_extension import ExtensionClassifier
from app.core.classifiers.by_filename import FilenameClassifier
from app.core.models import (
    ActionKind,
    Category,
    ClassifyOptions,
    ConflictKind,
    ConflictPolicy,
    FileEntry,
    OverrideSet,
    PlanItem,
    ScanScope,
    ScanSelection,
    SortPlan,
    Strategy,
)
from app.core.conflicts import TargetAllocator
from app.core.models import color_for_category
from app.core.overrides import ApplyReport, OverrideLayer
from app.core.rules import RuleEngine
from app.core.safety import SafetyGuard


@dataclass
class SpaceCheck:
    """跨卷空间校验结果。需求 9.10。"""

    ok: bool = True
    volume: str = ""
    required: int = 0
    free: int = 0

    @property
    def message(self) -> str:
        if self.ok:
            return ""
        return (
            f"目标卷 {self.volume} 剩余 {_human(self.free)}，"
            f"本次需要约 {_human(self.required)}（含 10% 余量），空间不足。"
        )


@dataclass
class PlanResult:
    plan: SortPlan
    space: SpaceCheck = field(default_factory=SpaceCheck)
    overrides: ApplyReport = field(default_factory=ApplyReport)


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


class Planner:
    """方案生成。"""

    def __init__(
        self,
        engine: RuleEngine | None = None,
        guard: SafetyGuard | None = None,
    ) -> None:
        self._engine = engine or RuleEngine()
        self._guard = guard or SafetyGuard()

    # -- 对外 -------------------------------------------------------------

    def build(
        self,
        entries: Sequence[FileEntry],
        *,
        root: Path,
        strategy: Strategy = Strategy.BY_TYPE,
        options: ClassifyOptions | None = None,
        conflict_policy: ConflictPolicy = ConflictPolicy.AUTO_RENAME,
        default_action: ActionKind = ActionKind.MOVE,
        overrides: OverrideSet | None = None,
        selection: ScanSelection | None = None,
        check_locked: bool = True,
    ) -> PlanResult:
        root = Path(root).resolve()
        options = options or ClassifyOptions(strategy=strategy)
        ctx = ClassifyContext(root=root, options=options)

        # 1) 分类
        suggestions: dict[str, Suggestion] = {}
        pipeline = self._pipeline_for(strategy)
        date_classifier = DateClassifier()
        for entry in entries:
            suggestion = pipeline.classify(entry, ctx)
            if strategy in STRATEGIES_WITH_DATE_SUFFIX and suggestion.category != UNCLASSIFIED:
                suggestion = self._append_date_level(entry, suggestion, ctx)
            elif strategy is Strategy.BY_DATE:
                dated = date_classifier.classify(entry, ctx)
                if dated is not None:
                    suggestion = dated
            suggestions[str(entry.path)] = suggestion

        # 2) 小类目合并（必须在 override 之前）
        if options.merge_small_categories:
            suggestions = self._merge_small(suggestions, options.small_category_threshold)

        # 3) override 叠加。override 是这条链上的独立输入，因此基础方案整体重建
        #    也不会冲掉手工调整（需求 19.3、19.13、19.14）。
        overrides = overrides or OverrideSet()
        resolved = OverrideLayer().resolve(
            suggestions, overrides, known_paths=[str(e.path) for e in entries]
        )

        # 4) + 5) 类目清洗、target 构造、冲突预检
        return self._materialize(
            entries=entries,
            suggestions=resolved.suggestions,
            included_overrides=resolved.included,
            color_overrides=resolved.colors,
            report=resolved.report,
            root=root,
            strategy=strategy,
            conflict_policy=conflict_policy,
            default_action=default_action,
            selection=selection,
            check_locked=check_locked,
        )

    # -- 内部 -------------------------------------------------------------

    def _pipeline_for(self, strategy: Strategy) -> ClassifierPipeline:
        available: dict[str, Classifier] = {
            FilenameClassifier(self._engine).name: FilenameClassifier(self._engine),
            ExtensionClassifier(self._engine).name: ExtensionClassifier(self._engine),
            SOURCE_DATE: DateClassifier(),
        }
        return build_pipeline(strategy, available)

    def _append_date_level(
        self, entry: FileEntry, suggestion: Suggestion, ctx: ClassifyContext
    ) -> Suggestion:
        """TYPE_AND_DATE：在类型类目后拼一级时间类目。需求 3.5。"""
        parts = date_parts(entry.mtime, ctx.options.date_granularity)
        if not parts:
            return suggestion
        # 只取最细一级，避免出现 文档/PDF/2024/2024-03 这样过深的路径
        combined = (*suggestion.category, parts[-1])
        return Suggestion(
            category=combined,
            confidence=suggestion.confidence,
            reason=f"{suggestion.reason}，再按修改时间分到 {parts[-1]}",
            source=suggestion.source,
        )

    @staticmethod
    def _merge_small(
        suggestions: dict[str, Suggestion], threshold: int
    ) -> dict[str, Suggestion]:
        """文件数低于阈值的类目并入「其他」。需求 3.12。

        `_未分类` 不参与合并：它本身就是兜底，并进「其他」只会让用户更难找到那些
        没被识别的文件。
        """
        counts: dict[tuple[str, ...], int] = defaultdict(int)
        for suggestion in suggestions.values():
            counts[suggestion.category] += 1

        small = {
            category
            for category, count in counts.items()
            if count < threshold and category != UNCLASSIFIED
        }
        if not small:
            return suggestions

        merged: dict[str, Suggestion] = {}
        for path, suggestion in suggestions.items():
            if suggestion.category in small:
                original = "/".join(suggestion.category)
                merged[path] = Suggestion(
                    category=OTHER,
                    confidence=suggestion.confidence,
                    reason=f"{suggestion.reason}；该类目文件数少于 {threshold}，已并入「其他」",
                    source=suggestion.source,
                )
            else:
                merged[path] = suggestion
        return merged

    def _materialize(
        self,
        *,
        entries: Sequence[FileEntry],
        suggestions: dict[str, Suggestion],
        included_overrides: dict[str, bool],
        color_overrides: dict[tuple[str, ...], str],
        report: ApplyReport,
        root: Path,
        strategy: Strategy,
        conflict_policy: ConflictPolicy,
        default_action: ActionKind,
        selection: ScanSelection | None,
        check_locked: bool,
    ) -> PlanResult:
        allocator = TargetAllocator(root=root, guard=self._guard)

        categories: dict[tuple[str, ...], Category] = {}
        items: list[PlanItem] = []
        unclassified: list[PlanItem] = []

        # 排序使输出确定：分类结果本身确定，构造顺序也必须确定，否则自动重命名的
        # 编号会随遍历顺序漂移（属性 17 的幂等性依赖这一点）。
        ordered = sorted(entries, key=lambda e: str(e.path))

        for entry in ordered:
            suggestion = suggestions.get(
                str(entry.path),
                Suggestion(UNCLASSIFIED, 0.0, "未参与分类", "fallback"),
            )
            cleaned = fsops.sanitize_parts(suggestion.category) or UNCLASSIFIED
            category = self._ensure_category(
                categories, cleaned, suggestion, color_overrides
            )

            category_dir = root.joinpath(*cleaned)
            target, conflict, renamed_from = allocator.allocate(
                source=entry.path,
                category_dir=category_dir,
                filename=entry.name,
                policy=conflict_policy,
                check_locked=check_locked,
            )

            action = self._decide_action(
                entry=entry,
                target=target,
                conflict=conflict,
                policy=conflict_policy,
                default_action=default_action,
            )

            item = PlanItem(
                entry=entry,
                category_id=category.id,
                target=target,
                action=action,
                conflict=conflict,
                confidence=suggestion.confidence,
                reason=suggestion.reason,
                included=True,
                renamed_from=renamed_from,
                by_llm=suggestion.source == "llm",
            )
            # 低置信度默认不勾选（需求 8.8）——由用户主动确认
            if suggestion.confidence < 0.6 and suggestion.source == "llm":
                item.included = False
            # 用户的勾选决定优先于任何自动判断（需求 19.5、19.13）
            override_included = included_overrides.get(str(entry.path))
            if override_included is not None:
                item.included = override_included

            if cleaned == UNCLASSIFIED:
                unclassified.append(item)
            else:
                items.append(item)

        plan = SortPlan(
            root=root,
            strategy=strategy,
            categories=self._ordered_categories(categories),
            items=items,
            unclassified=unclassified,
            scope=selection.scope() if selection else ScanScope.TOP_LEVEL_ONLY,
            selected_subfolders=selection.sorted_selected() if selection else (),
        )
        return PlanResult(
            plan=plan, space=self._check_space(plan), overrides=report
        )

    @staticmethod
    def _ensure_category(
        registry: dict[tuple[str, ...], Category],
        parts: tuple[str, ...],
        suggestion: Suggestion,
        color_overrides: dict[tuple[str, ...], str],
    ) -> Category:
        existing = registry.get(parts)
        if existing is not None:
            return existing
        category = Category.create(
            path_parts=parts,
            # 用户换过色就用用户的，否则按序号轮转取色
            color=color_overrides.get(parts)
            or color_for_category(parts, len(registry)),
            rule_source=suggestion.source,
        )
        registry[parts] = category
        return category

    @staticmethod
    def _ordered_categories(
        registry: dict[tuple[str, ...], Category]
    ) -> list[Category]:
        return [registry[key] for key in sorted(registry, key=lambda p: tuple(p))]

    @staticmethod
    def _decide_action(
        *,
        entry: FileEntry,
        target: Path,
        conflict: ConflictKind,
        policy: ConflictPolicy,
        default_action: ActionKind,
    ) -> ActionKind:
        # 幂等：已在目标类目目录里的文件不再搬动（需求 9.12）
        if entry.path.parent == target.parent:
            return ActionKind.SKIP
        if conflict in (ConflictKind.PATH_ESCAPE, ConflictKind.PATH_TOO_LONG, ConflictKind.LOCKED):
            return ActionKind.SKIP
        if conflict is ConflictKind.EXISTS and policy is ConflictPolicy.SKIP:
            return ActionKind.SKIP
        return default_action

    @staticmethod
    def _check_space(plan: SortPlan) -> SpaceCheck:
        """跨卷复制的空间校验。需求 9.10。"""
        needed: dict[str, int] = defaultdict(int)
        for item in plan.all_items():
            if not item.included or item.action is ActionKind.SKIP:
                continue
            if fsops.same_volume(item.entry.path, item.target):
                continue  # 同卷是原子重命名，不占额外空间
            anchor = item.target.anchor or str(item.target)
            needed[anchor] += item.entry.size

        for volume, required in needed.items():
            try:
                free = shutil.disk_usage(volume).free
            except OSError:
                continue
            if free < required * 1.1:
                return SpaceCheck(
                    ok=False, volume=volume, required=int(required * 1.1), free=free
                )
        return SpaceCheck()


class EmptyDirPredictor:
    """预测执行后会变空的目录。需求 20.7。

    与 Executor 的实际清理共用同一套条件与同一个纯函数
    ``fsops.is_predicted_empty``，因此预测清单与实际删除不可能出现偏差
    （属性 38）。
    """

    def predict(self, plan: SortPlan, selection: ScanSelection) -> list[Path]:
        candidates = selection.selected_closure()
        if not candidates:
            # 两层勾选缺一层，候选集合为空（需求 20.5、20.10）
            return []

        moving_out: dict[Path, set[Path]] = defaultdict(set)
        for item in plan.all_items():
            if not item.included or item.action is ActionKind.SKIP:
                continue
            moving_out[item.entry.path.parent].add(item.entry.path)

        predicted: list[Path] = []
        root = plan.root
        for directory in candidates:
            if directory == root:
                continue  # 根目录永不删除（需求 20.14）
            moved = moving_out.get(directory)
            if not moved:
                continue  # 本次没有文件从这里移出（需求 20.9）
            try:
                listing = frozenset(directory.iterdir())
            except OSError:
                continue
            if fsops.is_predicted_empty(listing, frozenset(moved)):
                predicted.append(directory)

        # 深度降序：先删子目录，父目录才有机会随之变空
        return sorted(predicted, key=lambda p: len(p.parts), reverse=True)
