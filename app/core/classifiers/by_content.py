"""按正文内容的关键词分类。priority 150，在 filename 之后、llm 之前。

需求 5.5：``text_head`` 非空时在其上应用关键词规则并产出类目建议。
``by_content`` 放在 ``智能`` 策略管线的第二位（filename 之后、llm 之前），
因为它比 LLM 快且零成本，但比 filename 慢（需要先提取正文）。
"""

from __future__ import annotations

from app.core.classifiers.base import SOURCE_CONTENT, ClassifyContext, Suggestion
from app.core.models import FileEntry
from app.core.rules import PRIORITY_KEYWORD, RuleEngine

CONFIDENCE_CONTENT_KEYWORD = 0.88


class ContentClassifier:
    """在 text_head 上应用关键词规则。需求 5.5。

    与 FilenameClassifier 类似，但匹配对象是文件正文而非文件名。
    正文关键词命中的置信度略低于文件名关键词（0.88 vs 0.9）：文件名
    含「发票」几乎不是巧合，正文含「发票」可能是引用或讨论。
    """

    name = SOURCE_CONTENT
    priority = PRIORITY_KEYWORD - 50  # 150，在 filename(200) 之后、extension(100) 之前

    def __init__(self, engine: RuleEngine) -> None:
        self._engine = engine

    def classify(self, entry: FileEntry, ctx: ClassifyContext) -> Suggestion | None:
        if not entry.text_head:
            return None

        hit = self._engine.match_text(entry.text_head)
        if hit is None:
            return None

        category_str = "/".join(hit.rule.category)
        if hit.rule.type == "regex":
            reason = f"正文匹配规则 {hit.rule.id} → {category_str}"
        else:
            reason = f"正文含「{hit.matched}」→ {category_str}"
        return Suggestion(
            category=hit.rule.category,
            confidence=CONFIDENCE_CONTENT_KEYWORD,
            reason=reason,
            source=SOURCE_CONTENT,
        )
