"""Classifier 协议、Suggestion 与 ClassifierPipeline。

## 确定性是结构性的，不是靠小心

需求 3.13 要求「同配置重复求值结果相同」。实现上的约束：管线内不使用集合迭代
顺序、不使用 ``hash()`` 相关顺序、不读时钟；日期类目只依赖 ``FileEntry.mtime``；
LLM 结果在一次重算内固定为传入的快照，管线里不发起请求。这些约束合起来让确定性
成为结构上必然，而不是每次改代码都要重新担心一遍的事。

## 为什么 by_date 不在 priority 序列里

其余分类器按 priority 降序竞争，第一个达到阈值的胜出。``by_date`` 不参与竞争：
``BY_DATE`` 策略下它是唯一来源，``TYPE_AND_DATE`` 策略下它作为第二级由 Planner
拼接（需求 3.5）。混进竞争序列会让「类型+时间」变成「类型或时间」。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, NamedTuple, Protocol, runtime_checkable

from app.core.models import ClassifyOptions, FileEntry, Strategy

#: 所有分类器都落空时的兜底类目。需求 3.8
UNCLASSIFIED: Final[tuple[str, ...]] = ("_未分类",)

#: 小类目合并的归集类目。需求 3.12
OTHER: Final[tuple[str, ...]] = ("其他",)

SOURCE_FILENAME = "filename"
SOURCE_CONTENT = "content"
SOURCE_LLM = "llm"
SOURCE_EXTENSION = "extension"
SOURCE_DATE = "date"
SOURCE_FALLBACK = "fallback"


class Suggestion(NamedTuple):
    """一条分类建议。

    ``reason`` 是可解释性的载体：用户凭它判断分类对不对，这直接决定他敢不敢点
    执行（需求 3.10）。所以每个分类器都必须给出人能读懂的理由，不能留空。
    """

    category: tuple[str, ...]
    confidence: float
    reason: str
    source: str


@dataclass
class ClassifyContext:
    """分类所需的全部外部信息。

    显式传入而非让分类器自己去取：分类器一旦能自己读配置或发请求，确定性就无法
    从结构上保证了。
    """

    root: object = None
    options: ClassifyOptions = field(default_factory=ClassifyOptions)
    #: 路径字符串 -> LLM 建议。一次重算内固定不变（需求 6.4、7.9）
    llm_suggestions: dict[str, Suggestion] = field(default_factory=dict)


@runtime_checkable
class Classifier(Protocol):
    name: str
    priority: int

    def classify(
        self, entry: FileEntry, ctx: ClassifyContext
    ) -> Suggestion | None: ...


#: 四种策略的分类器装配。需求 3.3-3.6
STRATEGY_PIPELINES: Final[dict[Strategy, tuple[str, ...]]] = {
    Strategy.BY_TYPE: (SOURCE_FILENAME, SOURCE_EXTENSION),
    Strategy.BY_DATE: (SOURCE_DATE,),
    Strategy.TYPE_AND_DATE: (SOURCE_FILENAME, SOURCE_EXTENSION),
    Strategy.SMART: (SOURCE_FILENAME, SOURCE_CONTENT, SOURCE_LLM, SOURCE_EXTENSION),
}

#: TYPE_AND_DATE 需要在类型类目之后再拼一级时间类目（需求 3.5）
STRATEGIES_WITH_DATE_SUFFIX: Final[frozenset[Strategy]] = frozenset(
    {Strategy.TYPE_AND_DATE}
)


class ClassifierPipeline:
    """按 priority 降序求值，采纳第一个达到阈值的建议。"""

    def __init__(self, classifiers: Sequence[Classifier] = ()) -> None:
        self._classifiers: list[Classifier] = []
        for classifier in classifiers:
            self.register(classifier)

    def register(self, classifier: Classifier) -> None:
        """注册分类器，按 priority 插入到正确位置。需求 3.11。

        同 priority 的按注册顺序排列（稳定排序），使求值次序确定。
        """
        self._classifiers.append(classifier)
        self._classifiers.sort(
            key=lambda c: -c.priority
        )  # list.sort 是稳定的，同 priority 保持注册顺序

    @property
    def classifiers(self) -> tuple[Classifier, ...]:
        return tuple(self._classifiers)

    def names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self._classifiers)

    def classify(self, entry: FileEntry, ctx: ClassifyContext) -> Suggestion:
        """产出一条建议，必定非 None。需求 3.7、3.8。

        全部落空或都低于阈值时返回 ``_未分类``——方案必须覆盖每一个文件
        （属性 12），所以这里不允许返回 None。
        """
        threshold = ctx.options.min_confidence
        best_below: Suggestion | None = None

        for classifier in self._classifiers:
            suggestion = classifier.classify(entry, ctx)
            if suggestion is None:
                continue
            if suggestion.confidence >= threshold:
                return suggestion
            if best_below is None or suggestion.confidence > best_below.confidence:
                best_below = suggestion

        reason = "没有规则命中"
        if best_below is not None:
            reason = (
                f"最高置信度 {best_below.confidence:.2f} 低于阈值 {threshold:.2f}"
                f"（{best_below.reason}）"
            )
        return Suggestion(UNCLASSIFIED, 0.0, reason, SOURCE_FALLBACK)


def build_pipeline(
    strategy: Strategy,
    available: dict[str, Classifier],
) -> ClassifierPipeline:
    """按策略装配管线。缺失的分类器被跳过。

    跳过而不报错是刻意的：``SMART`` 策略下 ``content`` 与 ``llm`` 可能因为
    「AI 未启用」或「正文提取未实现」而缺席，此时管线自然退化为
    filename → extension，方案仍然覆盖全部文件（需求 6.4、6.6）。
    """
    wanted = STRATEGY_PIPELINES.get(strategy, STRATEGY_PIPELINES[Strategy.BY_TYPE])
    return ClassifierPipeline(
        [available[name] for name in wanted if name in available]
    )
