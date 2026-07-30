"""OverrideLayer：把用户手工调整叠加到分类器结果之上。需求 19。

## 为什么 override 是独立输入而不是对方案的原地修改

重算时基础方案会被整体重建（规则改了、策略换了、AI 关了）。只有把 override 作为
规划链上的**独立输入**，「保留手工调整」才是结构上必然成立的，而不是靠每处重算
都记得去合并一次。这直接支撑重算稳定性（需求 19.14）与 override 优先性
（需求 19.13）。

## 四步顺序本身是正确性的一部分

    1) 类目级重映射   renamed_to / merged_into / color，构造 old → new 映射
    2) 类目重建       被引用但基础方案里已不存在的类目要重新建出来（需求 19.6）
    3) 条目级覆盖     按绝对路径覆盖类目与勾选状态（需求 19.5）
    4) 失效清理       路径已不存在的 override 被丢弃并计数（需求 19.7、19.8）

第 3 步必须在第 1 步之后：条目 override 记的可能是改名前的类目，得先把映射建好。
第 4 步必须最后：只有走完前三步才知道哪些 override 真的生效了。

## 与设计签名的差异

design.md 写的是 ``apply(base: SortPlan, overrides) -> (SortPlan, ApplyReport)``，
但它自己的 Planner 五步流水线又把 override 叠加放在**第 3 步、target 构造之前**。
两者不能同时成立——拿到 SortPlan 时 target 已经算好了，改类目就必须重算 target 与
冲突，等于把第 4、5 步再跑一遍。这里按流水线的顺序实现：本层只解析出「每个文件
最终归哪个类目、勾选与否」，target 构造与冲突预检仍由 Planner 统一做一次。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from app.core.classifiers.base import UNCLASSIFIED, Suggestion
from app.core.models import CategoryOverride, ItemOverride, OverrideSet

#: 防御 A→B→A 这类环状重映射
_MAX_REMAP_HOPS = 16


@dataclass
class ApplyReport:
    """叠加结果的说明，供 UI 如实告知用户。需求 19.6-19.8。"""

    #: 实际生效的 override 条数，即需求 19.8 的 N
    kept: int = 0
    #: 引用的文件已不存在而被丢弃的 override（需求 19.7）
    dropped_paths: list[str] = field(default_factory=list)
    #: 因基础方案里已不存在而被重建的类目（需求 19.6）
    rebuilt_categories: list[tuple[str, ...]] = field(default_factory=list)
    #: 类目级 override 生效条数
    category_hits: int = 0

    @property
    def dropped(self) -> int:
        return len(self.dropped_paths)

    def summary(self) -> str:
        text = f"已保留 {self.kept} 项手工调整"
        if self.dropped_paths:
            text += f"，{len(self.dropped_paths)} 项因文件已不存在被丢弃"
        return text


@dataclass
class ResolvedPlanInput:
    """叠加后的分类结果，交给 Planner 做 target 构造与冲突预检。"""

    suggestions: dict[str, Suggestion]
    included: dict[str, bool]
    colors: dict[tuple[str, ...], str]
    report: ApplyReport


class OverrideLayer:
    """override 叠加。纯函数：同样的输入必然产出同样的输出。"""

    def resolve(
        self,
        suggestions: Mapping[str, Suggestion],
        overrides: OverrideSet,
        known_paths: Iterable[str] | None = None,
    ) -> ResolvedPlanInput:
        paths = set(known_paths) if known_paths is not None else set(suggestions)
        report = ApplyReport()

        # ---- 1) 类目级重映射 ----
        remap, colors, deleted = self._build_category_maps(overrides)

        resolved: dict[str, Suggestion] = {}
        base_categories: set[tuple[str, ...]] = set()

        for path, suggestion in suggestions.items():
            category = suggestion.category
            base_categories.add(category)

            final = self._follow(category, remap)
            if final in deleted or category in deleted:
                # 需求 10.3：删除类目后其成员退回未分类
                resolved[path] = Suggestion(
                    category=UNCLASSIFIED,
                    confidence=suggestion.confidence,
                    reason=f"{suggestion.reason}；该类目已被删除，退回未分类",
                    source="user",
                )
                report.category_hits += 1
                continue

            if final != category:
                resolved[path] = Suggestion(
                    category=final,
                    confidence=suggestion.confidence,
                    reason=f"{suggestion.reason}；已按手工调整改为 {'/'.join(final)}",
                    source="user",
                )
                report.category_hits += 1
            else:
                resolved[path] = suggestion

        # ---- 3) 条目级覆盖 + 4) 失效清理 ----
        included: dict[str, bool] = {}
        for raw_path, item_override in overrides.items.items():
            if raw_path not in paths:
                report.dropped_paths.append(raw_path)
                continue

            applied = self._apply_item(
                raw_path, item_override, resolved, remap, deleted
            )
            if applied is not None:
                resolved[raw_path] = applied
            if item_override.included is not None:
                included[raw_path] = item_override.included
            report.kept += 1

        report.dropped_paths.sort()

        # ---- 2) 类目重建 ----
        # 走完覆盖后再看：哪些类目只因 override 而存在？它们就是被重建出来的
        # （需求 19.6）。这样判断比在第 2 步猜更准，因为此时最终类目集合已确定。
        final_categories = {s.category for s in resolved.values()}
        rebuilt = sorted(final_categories - base_categories)
        report.rebuilt_categories = list(rebuilt)

        # 用户显式创建的空类目也要保留其颜色
        for parts, category_override in overrides.categories.items():
            if category_override.color:
                target = self._follow(parts, remap)
                colors.setdefault(target, category_override.color)

        return ResolvedPlanInput(
            suggestions=resolved,
            included=included,
            colors=colors,
            report=report,
        )

    # -- 内部 -------------------------------------------------------------

    @staticmethod
    def _build_category_maps(
        overrides: OverrideSet,
    ) -> tuple[
        dict[tuple[str, ...], tuple[str, ...]],
        dict[tuple[str, ...], str],
        set[tuple[str, ...]],
    ]:
        remap: dict[tuple[str, ...], tuple[str, ...]] = {}
        colors: dict[tuple[str, ...], str] = {}
        deleted: set[tuple[str, ...]] = set()

        # 按 key 排序遍历，使结果不依赖字典插入顺序（确定性）
        for parts in sorted(overrides.categories):
            override: CategoryOverride = overrides.categories[parts]
            if override.deleted:
                deleted.add(parts)
                continue
            # merged_into 优先于 renamed_to：合并是更强的意图
            target = override.merged_into or override.renamed_to
            if target and target != parts:
                remap[parts] = target
            if override.color:
                colors[target or parts] = override.color
        return remap, colors, deleted

    @staticmethod
    def _follow(
        parts: tuple[str, ...],
        remap: Mapping[tuple[str, ...], tuple[str, ...]],
    ) -> tuple[str, ...]:
        """顺着重映射链走到终点。

        A 改名成 B、B 又被合并进 C 时，A 的成员应当落到 C。跳数设上限并记录访问
        过的节点，防止 A→B→A 这类环把调用方挂死。
        """
        seen = {parts}
        current = parts
        for _ in range(_MAX_REMAP_HOPS):
            nxt = remap.get(current)
            if nxt is None or nxt in seen:
                return current
            seen.add(nxt)
            current = nxt
        return current

    @staticmethod
    def _apply_item(
        path: str,
        override: ItemOverride,
        resolved: Mapping[str, Suggestion],
        remap: Mapping[tuple[str, ...], tuple[str, ...]],
        deleted: set[tuple[str, ...]],
    ) -> Suggestion | None:
        if override.category_path is None:
            return None

        target = OverrideLayer._follow(override.category_path, remap)
        if target in deleted:
            target = UNCLASSIFIED

        previous = resolved.get(path)
        base_reason = previous.reason if previous else "无分类器结果"
        confidence = previous.confidence if previous else 1.0

        return Suggestion(
            category=target,
            confidence=confidence,
            reason=f"手工指定为 {'/'.join(target)}（原：{base_reason}）",
            source="user",
        )
