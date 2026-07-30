"""按扩展名分类。priority 100，兜底但仍高于默认阈值 0.6。"""

from __future__ import annotations

from app.core.classifiers.base import SOURCE_EXTENSION, ClassifyContext, Suggestion
from app.core.models import FileEntry
from app.core.rules import PRIORITY_EXTENSION, RuleEngine

CONFIDENCE = 0.7


class ExtensionClassifier:
    """扩展名规则。需求 4.7。"""

    name = SOURCE_EXTENSION
    priority = PRIORITY_EXTENSION

    def __init__(self, engine: RuleEngine) -> None:
        self._engine = engine

    def classify(self, entry: FileEntry, ctx: ClassifyContext) -> Suggestion | None:
        if not entry.ext:
            return None
        hit = self._engine.match_ext(entry.ext)
        if hit is None:
            return None
        category = "/".join(hit.rule.category)
        return Suggestion(
            category=hit.rule.category,
            confidence=CONFIDENCE,
            reason=f"扩展名 {entry.ext} → {category}",
            source=SOURCE_EXTENSION,
        )
