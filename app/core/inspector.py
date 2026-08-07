"""ContentExtractor：受支持文档的正文片段提取。

需求 5（内容级分类）只关心「文件里写了什么」，不关心排版。因此每个读取器都是
**读到 4096 字符就停**的惰性实现：pypdf 逐页累加、python-docx 逐段落累加、
openpyxl 以 ``read_only=True`` 逐行取单元格文本、python-pptx 逐形状取
``text_frame``、纯文本先读前 64KB 字节再用 chardet 探测编码解码。

门槛与上限（需求 5.1、5.2）：
    仅当扩展名属于 pdf/docx/xlsx/pptx/txt/md 且 size 小于 20MB 时提取，
    否则 ``text_head`` 保持为空。任一读取器抛异常都捕获并写入 ``error`` 后
    继续处理其余文件（需求 5.3）。

并发在扫描线程内用 ``ThreadPoolExecutor(max_workers=8)`` 跑（需求 5.4），
提交前检查 ``CancelToken``。
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import chardet

from app.core.models import FileEntry
from app.core.progress import CancelToken, NullCancelToken

#: 支持的扩展名 -> 读取器。需求 5.1。
SUPPORTED_EXTENSIONS = frozenset({".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md"})

#: 文件大小上限：20MB。需求 5.2。
MAX_SIZE_BYTES = 20 * 1024 * 1024

#: 提取的正文上限：4KB。需求 5.1。
MAX_CHARS = 4096

#: 纯文本预读字节数（用于编码探测）。需求 5.6。
TEXT_PEEK_BYTES = 64 * 1024

#: 最大并发工作线程。需求 5.4。
MAX_WORKERS = 8


@dataclass
class ExtractResult:
    """单个文件的提取结果。"""

    path: Path
    text_head: str | None = None
    error: str | None = None


def _extract_pdf(path: Path) -> str:
    """逐页累加，达到 MAX_CHARS 即停。"""
    from pypdf import PdfReader

    parts: list[str] = []
    reader = PdfReader(str(path))
    total = 0
    for page in reader.pages:
        text = page.extract_text() or ""
        parts.append(text)
        total += len(text)
        if total >= MAX_CHARS:
            break
    return "".join(parts)[:MAX_CHARS]


def _extract_docx(path: Path) -> str:
    """逐段落累加，达到 MAX_CHARS 即停。"""
    from docx import Document

    parts: list[str] = []
    total = 0
    doc = Document(str(path))
    for para in doc.paragraphs:
        parts.append(para.text)
        total += len(para.text)
        if total >= MAX_CHARS:
            break
    return "\n".join(parts)[:MAX_CHARS]


def _extract_xlsx(path: Path) -> str:
    """以 read_only 模式逐行取单元格文本，达到 MAX_CHARS 即停。"""
    from openpyxl import load_workbook

    parts: list[str] = []
    total = 0
    wb = load_workbook(str(path), read_only=True, data_only=True)
    for sheet in wb.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                value = cell.value
                if value is not None:
                    text = str(value)
                    parts.append(text)
                    total += len(text)
                    if total >= MAX_CHARS:
                        wb.close()
                        return "\n".join(parts)[:MAX_CHARS]
    wb.close()
    return "\n".join(parts)[:MAX_CHARS]


def _extract_pptx(path: Path) -> str:
    """逐形状取 text_frame 文本，达到 MAX_CHARS 即停。"""
    from pptx import Presentation

    parts: list[str] = []
    total = 0
    prs = Presentation(str(path))
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    text = para.text
                    parts.append(text)
                    total += len(text)
                    if total >= MAX_CHARS:
                        return "\n".join(parts)[:MAX_CHARS]
    return "\n".join(parts)[:MAX_CHARS]


def _extract_text(path: Path) -> str:
    """纯文本：先读前 64KB 用 chardet 探测编码，再解码前 MAX_CHARS 字符。"""
    raw = os.path.getsize(path)
    read_size = min(raw, TEXT_PEEK_BYTES)
    with open(path, "rb") as f:
        head = f.read(read_size)

    detected = chardet.detect(head)
    encoding = detected.get("encoding") or "utf-8"
    # chardet 可能返回无意义结果，用 errors="replace" 兜底
    with open(path, "r", encoding=encoding, errors="replace") as f:
        return f.read(MAX_CHARS)


def _extract_md(path: Path) -> str:
    """Markdown 按纯文本处理。"""
    return _extract_text(path)


#: 扩展名到读取器的分派表。
_READERS = {
    ".pdf": _extract_pdf,
    ".docx": _extract_docx,
    ".xlsx": _extract_xlsx,
    ".pptx": _extract_pptx,
    ".txt": _extract_text,
    ".md": _extract_md,
}


def _should_extract(entry: FileEntry) -> bool:
    """是否应该提取：扩展名受支持且大小低于上限。需求 5.1、5.2。"""
    if entry.ext not in SUPPORTED_EXTENSIONS:
        return False
    if entry.size >= MAX_SIZE_BYTES:
        return False
    return True


def _extract_one(entry: FileEntry) -> ExtractResult:
    """对单个文件执行提取，所有异常都捕获。需求 5.3。"""
    try:
        reader = _READERS.get(entry.ext)
        if reader is None:
            return ExtractResult(path=entry.path)
        text = reader(entry.path)
        return ExtractResult(path=entry.path, text_head=text)
    except Exception as exc:  # noqa: BLE001 — 需求 5.3 要求继续处理其余文件
        return ExtractResult(path=entry.path, error=f"内容提取失败: {exc}")


def extract_for_entries(
    entries: list[FileEntry],
    cancel: CancelToken | None = None,
    max_workers: int = MAX_WORKERS,
) -> list[ExtractResult]:
    """对一批 FileEntry 并发提取正文片段。

    仅对应该提取的条目调用读取器；其余直接返回空结果。
    结果顺序与输入无关，调用方按 ``path`` 匹配。
    """
    if cancel is None:
        cancel = NullCancelToken()

    to_extract = [e for e in entries if _should_extract(e)]
    results: list[ExtractResult] = []

    # 不需要提取时直接返回，避免拉起线程池
    if not to_extract:
        return results

    # 单线程跑少量文件不值得线程池开销
    if len(to_extract) <= 2:
        for entry in to_extract:
            if cancel.cancelled:
                break
            results.append(_extract_one(entry))
        return results

    with ThreadPoolExecutor(max_workers=min(max_workers, len(to_extract))) as pool:
        futures = {pool.submit(_extract_one, e): e for e in to_extract}
        for future in as_completed(futures):
            if cancel.cancelled:
                break
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001
                entry = futures[future]
                results.append(
                    ExtractResult(path=entry.path, error=f"内容提取失败: {exc}")
                )

    return results
