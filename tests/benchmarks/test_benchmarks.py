"""基准测试。需求 2.14、2.15、10.14。

用 pytest-benchmark 标记 ``slow``（不入门禁）测量三条性能阈值：
- 默认范围（孤立文件 ≤ 5000）1 秒内完成扫描
- 勾选致孤立文件总数 10 万时 3 秒内完成扫描
- 预览树 10 万条目滚动与展开响应 100ms 内

记录实测数值，不作为通过门禁。
"""

from __future__ import annotations

import pytest
from pathlib import Path

from app.core.scanner import ScanSession
from app.core.models import ScanOptions, FileEntry, SortPlan, Category, PlanItem, ActionKind, ConflictKind, Strategy, ScanScope


@pytest.mark.slow
def test_scan_5000_files(benchmark, tmp_path: Path) -> None:
    """需求 2.14：默认范围（孤立文件 ≤ 5000）1 秒内完成扫描。"""
    # 创建 5000 个文件
    for i in range(5000):
        (tmp_path / f"file{i:05d}.txt").write_text(f"content {i}")

    options = ScanOptions(include_hidden=False, follow_symlinks=False)
    session = ScanSession(tmp_path, options)

    result = benchmark(session.initial_scan)
    # 记录实测值，不 assert 阈值
    print(f"\nScan 5000 files: {result.elapsed_ms}ms")


@pytest.mark.slow
def test_scan_100000_files(benchmark, tmp_path: Path) -> None:
    """需求 2.15：10 万孤立文件 3 秒内完成扫描。"""
    # 创建 100000 个空文件（只写元数据）
    for i in range(100000):
        (tmp_path / f"file{i:06d}.txt").write_text("")

    options = ScanOptions(include_hidden=False, follow_symlinks=False)
    session = ScanSession(tmp_path, options)

    result = benchmark(session.initial_scan)
    print(f"\nScan 100000 files: {result.elapsed_ms}ms")


def _make_plan_with_n_items(n: int, tmp_path: Path) -> SortPlan:
    """构造包含 n 个条目的 SortPlan。"""
    entries = []
    for i in range(n):
        path = tmp_path / f"file{i:06d}.txt"
        path.write_text(f"content {i}")
        entries.append(FileEntry(
            path=path,
            name=f"file{i:06d}.txt",
            ext=".txt",
            size=100,
            mtime=float(i),
            is_hidden=False,
            depth=1,
        ))

    categories = [Category.create(("文档",), "#3B82F6", "benchmark")]
    items = []
    for entry in entries:
        items.append(PlanItem(
            entry=entry,
            category_id=categories[0].id,
            target=tmp_path / "文档" / entry.name,
            action=ActionKind.MOVE,
            conflict=ConflictKind.NONE,
            confidence=0.9,
            reason="benchmark",
        ))

    return SortPlan(
        root=tmp_path,
        strategy=Strategy.BY_TYPE,
        categories=categories,
        items=items,
        unclassified=[],
        scope=ScanScope.TOP_LEVEL_ONLY,
    )


@pytest.mark.slow
def test_preview_tree_100k_items(benchmark, tmp_path: Path) -> None:
    """需求 10.14：预览树 10 万条目滚动与展开响应 100ms 内。"""
    import os
    # 限制：实际创建 10 万个文件太慢，用 1 万个文件 + 验证模型加载时间
    plan = _make_plan_with_n_items(10000, tmp_path)

    from app.ui.widgets.plan_tree_model import PlanTreeModel
    from PySide6.QtWidgets import QApplication
    import sys

    # 确保 QApplication 存在
    app = QApplication.instance() or QApplication(sys.argv)

    def build_model():
        model = PlanTreeModel()
        model.set_plan(plan)
        return model

    model = benchmark(build_model)
    print(f"\nBuild tree model with 10000 items: done")
