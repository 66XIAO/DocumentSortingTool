"""by_content 分类器单元测试。"""

from __future__ import annotations

from pathlib import Path

from app.core.classifiers.by_content import CONFIDENCE_CONTENT_KEYWORD, ContentClassifier
from app.core.classifiers.base import SOURCE_CONTENT
from app.core.models import FileEntry
from app.core.rules import RuleEngine, builtin_rules


def _entry(name: str, text_head: str | None = None, ext: str = ".txt") -> FileEntry:
    return FileEntry(
        path=Path(f"/fake/{name}"),
        name=name,
        ext=ext,
        size=100,
        mtime=0.0,
        is_hidden=False,
        depth=1,
        text_head=text_head,
    )


def _classifier() -> ContentClassifier:
    return ContentClassifier(RuleEngine(builtin_rules()))


class TestContentClassifier:
    def test_name_and_priority(self) -> None:
        clf = _classifier()
        assert clf.name == SOURCE_CONTENT
        assert clf.priority == 150  # 在 filename(200) 之后

    def test_no_text_head_returns_none(self) -> None:
        clf = _classifier()
        entry = _entry("a.txt", text_head=None)
        assert clf.classify(entry, __import__('app.core.classifiers.base', fromlist=['ClassifyContext']).ClassifyContext()) is None

    def test_empty_text_head_returns_none(self) -> None:
        clf = _classifier()
        from app.core.classifiers.base import ClassifyContext
        entry = _entry("a.txt", text_head="")
        assert clf.classify(entry, ClassifyContext()) is None

    def test_keyword_match_in_text(self) -> None:
        clf = _classifier()
        from app.core.classifiers.base import ClassifyContext
        entry = _entry("a.txt", text_head="这是一份报销单据")
        result = clf.classify(entry, ClassifyContext())
        assert result is not None
        assert "报销" in result.reason
        assert result.confidence == CONFIDENCE_CONTENT_KEYWORD
        assert result.source == SOURCE_CONTENT

    def test_no_match_returns_none(self) -> None:
        clf = _classifier()
        from app.core.classifiers.base import ClassifyContext
        entry = _entry("a.txt", text_head="今天天气真好")
        assert clf.classify(entry, ClassifyContext()) is None

    def test_case_insensitive(self) -> None:
        clf = _classifier()
        from app.core.classifiers.base import ClassifyContext
        entry = _entry("a.txt", text_head="这份合同很重要")
        result = clf.classify(entry, ClassifyContext())
        assert result is not None
        assert "合同" in result.reason

    def test_multiple_keywords_picks_first_rule(self) -> None:
        clf = _classifier()
        from app.core.classifiers.base import ClassifyContext
        # 报销和发票都在文本中，应该按规则顺序匹配第一个
        entry = _entry("a.txt", text_head="发票和报销都在")
        result = clf.classify(entry, ClassifyContext())
        assert result is not None
        assert result.source == SOURCE_CONTENT
