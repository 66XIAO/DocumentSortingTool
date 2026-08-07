"""属性 11：正文提取门槛、上限与编码正确性。

需求 5.1、5.2、5.5、5.6。
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.inspector import (
    MAX_CHARS,
    MAX_SIZE_BYTES,
    SUPPORTED_EXTENSIONS,
    _should_extract,
)
from app.core.models import FileEntry


@given(ext=st.text(min_size=1, max_size=5), size=st.integers(min_value=0, max_value=MAX_SIZE_BYTES + 1000))
@settings(max_examples=100)
def test_should_extract_respects_thresholds(ext: str, size: int) -> None:
    """门槛与上限：扩展名不受支持或 size >= 20MB 时跳过。"""
    entry = FileEntry(
        path=Path(f"/tmp/test.{ext}"),
        name=f"test.{ext}",
        ext=f".{ext.lower()}",
        size=size,
        mtime=0.0,
        is_hidden=False,
        depth=1,
    )
    supported = f".{ext.lower()}" in SUPPORTED_EXTENSIONS
    under_limit = size < MAX_SIZE_BYTES

    expected = supported and under_limit
    assert _should_extract(entry) is expected


@given(data=st.binary(min_size=0, max_size=1024))
@settings(max_examples=50)
def test_extract_never_raises(data: bytes) -> None:
    """任一读取器抛异常都捕获，不向上传播。"""
    import tempfile
    from pathlib import Path

    from app.core.inspector import extract_for_entries

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.txt"
        path.write_bytes(data)

        entry = FileEntry(
            path=path,
            name="test.txt",
            ext=".txt",
            size=len(data),
            mtime=0.0,
            is_hidden=False,
            depth=1,
        )
        # 不应抛异常
        results = extract_for_entries([entry])
        # 结果中 text_head 要么为空（空文件）要么非空
        for r in results:
            if r.text_head is not None:
                assert isinstance(r.text_head, str)


@given(text=st.text(min_size=0, max_size=MAX_CHARS * 3))
@settings(max_examples=50)
def test_text_head_bounded(text: str) -> None:
    """提取的正文不超过 MAX_CHARS。"""
    import tempfile
    from pathlib import Path

    from app.core.inspector import extract_for_entries

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.txt"
        path.write_text(text, encoding="utf-8")

        entry = FileEntry(
            path=path,
            name="test.txt",
            ext=".txt",
            size=path.stat().st_size,
            mtime=0.0,
            is_hidden=False,
            depth=1,
        )
        results = extract_for_entries([entry])
        for r in results:
            if r.text_head is not None:
                assert len(r.text_head) <= MAX_CHARS
