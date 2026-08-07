"""隐私分档单元测试。需求 8.1-8.4。"""

from __future__ import annotations

from pathlib import Path

from app.core.llm.taxonomy import make_summary, make_summaries, HEAD_CHARS
from app.core.models import FileEntry, PrivacyLevel


def _entry(name: str = "test.txt", text_head: str | None = None) -> FileEntry:
    return FileEntry(
        path=Path(f"/root/sub/{name}"),
        name=name,
        ext=".txt",
        size=1024,
        mtime=1700000000.0,
        is_hidden=False,
        depth=2,
        text_head=text_head,
    )


class TestMakeSummary:
    def test_metadata_only_excludes_text(self) -> None:
        """需求 8.1：仅元数据档不发送正文。"""
        entry = _entry(text_head="这是正文内容" * 100)
        summary = make_summary(entry, root=Path("/root"), privacy=PrivacyLevel.METADATA_ONLY)
        assert summary.text_head is None
        assert summary.name == "test.txt"
        assert summary.ext == ".txt"
        assert summary.size == 1024

    def test_metadata_plus_head_includes_truncated_text(self) -> None:
        """需求 8.2：元数据+正文前 500 字档发送前 500 字符。"""
        long_text = "A" * 1000
        entry = _entry(text_head=long_text)
        summary = make_summary(entry, root=Path("/root"), privacy=PrivacyLevel.METADATA_PLUS_HEAD500)
        assert summary.text_head is not None
        assert len(summary.text_head) == HEAD_CHARS

    def test_relative_path(self) -> None:
        """需求 8.4：绝对路径替换为相对路径。"""
        entry = _entry()
        summary = make_summary(entry, root=Path("/root"), privacy=PrivacyLevel.METADATA_ONLY)
        assert summary.rel_path.replace("\\", "/") == "sub/test.txt"
        # 不应包含绝对路径
        assert "/root" not in summary.rel_path or summary.rel_path.startswith("sub/")

    def test_no_absolute_path_leak(self) -> None:
        """需求 8.4：序列化文本不含根目录字符串。"""
        entry = _entry()
        summary = make_summary(entry, root=Path("/secret/path"), privacy=PrivacyLevel.METADATA_ONLY)
        assert "/secret/path" not in summary.rel_path
        assert "secret" not in summary.rel_path

    def test_metadata_plus_head_no_text_head(self) -> None:
        """正文提取为空时，即使高档也不发送正文。"""
        entry = _entry(text_head=None)
        summary = make_summary(entry, root=Path("/root"), privacy=PrivacyLevel.METADATA_PLUS_HEAD500)
        assert summary.text_head is None


class TestMakeSummaries:
    def test_batch(self) -> None:
        entries = [_entry("a.txt"), _entry("b.txt")]
        summaries = make_summaries(entries, root=Path("/root"), privacy=PrivacyLevel.METADATA_ONLY)
        assert len(summaries) == 2
        assert summaries[0].name == "a.txt"
        assert summaries[1].name == "b.txt"

    def test_empty(self) -> None:
        assert make_summaries([], root=Path("/root"), privacy=PrivacyLevel.METADATA_ONLY) == []
