"""按文件名的关键词与正则分类。priority 200，高于扩展名（需求 3.9）。"""

from __future__ import annotations

from app.core.classifiers.base import SOURCE_FILENAME, ClassifyContext, Suggestion
from app.core.models import FileEntry
from app.core.rules import PRIORITY_KEYWORD, RuleEngine

CONFIDENCE_KEYWORD = 0.9
CONFIDENCE_REGEX = 0.85


class FilenameClassifier:
    """文件名规则。需求 4.6、4.9。

    关键词命中的置信度略高于正则：关键词是用户自己写下的词，「发票」出现在文件名
    里几乎不会是巧合；正则更容易误伤（`image_1.png` 未必是截图）。
    """

    name = SOURCE_FILENAME
    priority = PRIORITY_KEYWORD

    def __init__(self, engine: RuleEngine) -> None:
        self._engine = engine

    def classify(self, entry: FileEntry, ctx: ClassifyContext) -> Suggestion | None:
        hit = self._engine.match_name(entry.name)
        if hit is None:
            return None
        category = "/".join(hit.rule.category)
        if hit.rule.type == "regex":
            reason = f"文件名匹配规则 {hit.rule.id} → {category}"
            confidence = CONFIDENCE_REGEX
        else:
            reason = f"文件名含「{hit.matched}」→ {category}"
            confidence = CONFIDENCE_KEYWORD
        return Suggestion(
            category=hit.rule.category,
            confidence=confidence,
            reason=reason,
            source=SOURCE_FILENAME,
        )
