"""属性 14：隐私与凭据不外泄。

需求 8.1-8.4。
"""

from __future__ import annotations

import json
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.llm.taxonomy import make_summary
from app.core.models import FileEntry, PrivacyLevel


@st.composite
def file_entries(draw: st.DrawFn) -> FileEntry:
    """生成测试用 FileEntry。"""
    name = draw(st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("L", "N"))))
    text = draw(st.one_of(st.none(), st.text(min_size=0, max_size=2000)))
    path = draw(st.text(min_size=5, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N"))))
    return FileEntry(
        path=Path(f"/secret/{path}/{name}.txt"),
        name=f"{name}.txt",
        ext=".txt",
        size=draw(st.integers(min_value=0, max_value=10000)),
        mtime=0.0,
        is_hidden=False,
        depth=2,
        text_head=text,
    )


@given(entry=file_entries())
@settings(max_examples=100)
def test_metadata_only_no_text_leak(entry: FileEntry) -> None:
    """属性 14：仅元数据档不泄露正文。"""
    summary = make_summary(entry, root=Path("/secret"), privacy=PrivacyLevel.METADATA_ONLY)
    assert summary.text_head is None


@given(entry=file_entries())
@settings(max_examples=100)
def test_no_absolute_path_leak(entry: FileEntry) -> None:
    """属性 14：摘要中不含绝对路径（根目录字符串）。"""
    root = Path("/very/secret/path")
    summary = make_summary(entry, root=root, privacy=PrivacyLevel.METADATA_ONLY)
    # rel_path 不应包含根目录的任何一段
    assert "very" not in summary.rel_path
    assert "secret" not in summary.rel_path
    # 序列化后也不应泄露
    payload = json.dumps({"rel_path": summary.rel_path, "name": summary.name})
    assert "very/secret" not in payload.replace("\\", "/")


@given(entry=file_entries())
@settings(max_examples=100)
def test_metadata_plus_head_bounded(entry: FileEntry) -> None:
    """属性 14：高档正文不超过 500 字符。"""
    summary = make_summary(entry, root=Path("/root"), privacy=PrivacyLevel.METADATA_PLUS_HEAD500)
    if summary.text_head is not None:
        assert len(summary.text_head) <= 500
