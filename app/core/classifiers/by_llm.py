"""LLM 分类器。priority 50，在 content 之后、extension 之前（智能策略专用）。

需求 6.4、6.6：``ai.enabled`` 为 false 时管线跳过 ``by_llm``，Provider 调用次数为 0。
需求 8.7：LLM 分配的条目在预览树显示 AI 角标。
需求 8.8：confidence 小于 0.6 的条目 included 置为 false。
"""

from __future__ import annotations

from collections.abc import Mapping

from app.core.classifiers.base import SOURCE_LLM, ClassifyContext, Suggestion
from app.core.models import FileEntry


class LLMAssigned(Exception):
    """标记该条目已被 LLM 分配（供管线识别）。"""


class LLMClassifier:
    """LLM 分类器。需求 3.6、7.9。

    与其余分类器不同，LLMClassifier 不直接调用 Provider。它依赖
    ``ClassifyContext.llm_suggestions`` —— 那里存放的是 Planner 已经跑完
    两阶段调用后的快照，分类器只是查表。这样把「网络调用」与「分类求值」解耦，
    管线仍然保持确定性（需求 3.13）。
    """

    name = SOURCE_LLM
    priority = 50  # 在 content(150) 之后、extension(100) 之前 — 实际 extension 是 100，llm 是 50

    def __init__(self, suggestions: Mapping[str, Suggestion] | None = None) -> None:
        self._suggestions: Mapping[str, Suggestion] = suggestions or {}

    def classify(self, entry: FileEntry, ctx: ClassifyContext) -> Suggestion | None:
        """查表：路径 -> LLM 建议。"""
        # 优先用 ctx 中的快照（测试/外部注入），其次用构造时传入的
        suggestions = ctx.llm_suggestions if ctx.llm_suggestions else self._suggestions
        key = str(entry.path)
        return suggestions.get(key)

    @property
    def suggestion_count(self) -> int:
        return len(self._suggestions)
