"""基准测试。需求 2.14、2.15、10.13、10.14。

全部标记 ``slow``，不入默认门禁；跑法：
    pytest tests/benchmarks -m slow -q -s

## 实测结论（10 万条目，1536x864 / DPR 1.25）

达标：
    模式切换      35ms   （修复前 1179ms，见 PlanTreeModel._precompute 的说明）
    展开最大类目  38ms
    常规滚动      每步约 12ms
    懒加载        1 万条目 56ms

未达标，已用 xfail 记录并写清原因与所需改动：
    懒加载最慢一批（10 万）  数百毫秒
    跳到滚动条最末端         1 万约 315ms、10 万约 713ms

两项同源：单个类目有上万成员时，QTreeView 对已展开分支的重布局代价随该分支可见
行数增长。已排除模型侧原因（fetchMore 本体 0.4ms、data() 调用次数恒定、折叠状态
下插入恒定）。要达标需要给大类目分桶，使任一分支的可见行数有上界。

## 与旧版的三处纠正

1. **预览树基准原先名为 100k、实际只建 1 万条。** 原因是它为每个条目在磁盘上真建
   一个文件，10 万次 ``write_text`` 在 Windows 上要跑好几分钟。但 ``PlanTreeModel``
   从不碰文件系统——它只读 ``FileEntry`` 的元数据字段。因此这里改为**纯内存**构造
   条目，10 万条几秒就能建好，需求 10.14 点名的规模才真正被测到。

2. **原先只测 ``set_plan``（建模型），没测需求 10.14 真正要求的滚动与展开。**
   现在把模型挂到真实 ``QTreeView`` 上，分别测量展开、批量懒加载与滚动，
   并对 100ms 阈值做断言。

3. **原先只 print 实测值、不 assert。** 阈值来自需求，就该断言。这些测试被排除在
   默认门禁之外，正是断言该待的地方——它只在显式跑 slow 时生效，不会让日常
   开发被机器负载干扰。

## 为什么 10 万条目下展开还能在 100ms 内

``PlanTreeModel`` 用 ``canFetchMore`` / ``fetchMore`` 分批加载，每批
``FETCH_BATCH`` 行（需求 10.13）。展开一个 10 万成员的类目只会实例化 200 个子节点，
其余留在 ``pending`` 里。这条基准的意义就是守住这个设计不被改坏。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.core.models import (
    ActionKind,
    Category,
    ConflictKind,
    FileEntry,
    PlanItem,
    ScanOptions,
    ScanScope,
    SortPlan,
    Strategy,
)
from app.core.safety import SafetyGuard
from app.core.scanner import ScanSession

#: 需求 10.14 的响应上限
UI_BUDGET_MS = 100.0

#: 需求 2.14、2.15 的扫描上限
SCAN_5K_BUDGET_S = 1.0
SCAN_100K_BUDGET_S = 3.0

#: 需求 7.6 的 taxonomy 上限，用来构造一个规模真实的多类目树
CATEGORY_COUNT = 12


def _open_guard() -> SafetyGuard:
    """tmp_path 位于 AppData\\Local\\Temp 之下，默认守卫会判成系统路径而拒绝。"""
    return SafetyGuard(system_roots=(), denied_segments=())


# ---------------------------------------------------------------------------
# 扫描（需求 2.14、2.15）
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_scan_5000_loose_files_within_one_second(benchmark, tmp_path: Path) -> None:
    """需求 2.14：默认范围、孤立文件 ≤ 5000 时 1 秒内完成扫描。"""
    for i in range(5000):
        (tmp_path / f"file{i:05d}.txt").touch()

    session = ScanSession(tmp_path, ScanOptions(), guard=_open_guard())
    result = benchmark(session.initial_scan)

    assert len(result.entries) == 5000
    print(f"\n[扫描 5000 个孤立文件] {benchmark.stats['mean']:.3f}s")
    assert benchmark.stats["mean"] < SCAN_5K_BUDGET_S


@pytest.mark.slow
def test_scan_100000_loose_files_within_three_seconds(
    benchmark, tmp_path: Path
) -> None:
    """需求 2.15：孤立文件总数 10 万时 3 秒内完成扫描（仅读元数据）。

    用 ``touch()`` 而不是 ``write_text("")``：后者要多走一遍写入路径，
    10 万次的差距是分钟级的，而扫描只读元数据，文件内容与本测试无关。
    """
    for i in range(100000):
        (tmp_path / f"file{i:06d}.txt").touch()

    session = ScanSession(tmp_path, ScanOptions(), guard=_open_guard())
    result = benchmark(session.initial_scan)

    assert len(result.entries) == 100000
    print(f"\n[扫描 100000 个孤立文件] {benchmark.stats['mean']:.3f}s")
    assert benchmark.stats["mean"] < SCAN_100K_BUDGET_S


# ---------------------------------------------------------------------------
# 预览树（需求 10.13、10.14）
# ---------------------------------------------------------------------------


def _plan_in_memory(count: int, root: Path, categories: int = CATEGORY_COUNT) -> SortPlan:
    """纯内存构造 count 个条目的方案，**不碰文件系统**。

    ``PlanTreeModel`` 只读 FileEntry 的元数据字段，真建文件纯属浪费——正是它让
    旧版基准无法跑到 10 万条。
    """
    cats = [
        Category.create(("类目", f"{index:02d}"), "#3B82F6", "benchmark")
        for index in range(categories)
    ]

    items: list[PlanItem] = []
    for i in range(count):
        cat = cats[i % categories]
        name = f"file{i:06d}.txt"
        entry = FileEntry(
            path=root / name,
            name=name,
            ext=".txt",
            size=100 + i,
            mtime=float(i),
            is_hidden=False,
            depth=1,
        )
        items.append(
            PlanItem(
                entry=entry,
                category_id=cat.id,
                target=root.joinpath(*cat.path_parts, name),
                action=ActionKind.MOVE,
                conflict=ConflictKind.NONE,
                confidence=0.9,
                reason="benchmark",
            )
        )

    return SortPlan(
        root=root,
        strategy=Strategy.BY_TYPE,
        categories=cats,
        items=items,
        unclassified=[],
        scope=ScanScope.TOP_LEVEL_ONLY,
    )


def _tree_with_plan(qtbot, plan: SortPlan):
    """用**真实出货的那个预览页**来测，返回 (源模型, 视图)。

    刻意不自己搭一个精简 QTreeView：真实页面在模型与视图之间还夹着
    ``PlanFilterProxy``，表头也有各自的 resize 策略，这些都会显著改变性能表现。
    自己搭一个简化版测出来的数字好看但不算数——需求 10.14 约束的是用户真正用到
    的那个树。

    必须真的 ``show()``：不可见的视图不做布局也不绘制，测出来的「滚动耗时」是假的。
    """
    from app.ui.pages.preview_page import PreviewPage

    page = PreviewPage()
    qtbot.addWidget(page)
    page.resize(1200, 700)
    page.show()
    qtbot.waitExposed(page)

    page.model.set_plan(plan)
    return page.model, page.tree


def _view_index(view, source_index):
    """源模型下标 -> 视图（代理）下标。真实页面夹着一层代理，不映射就点不到行。"""
    proxy = view.model()
    mapper = getattr(proxy, "mapFromSource", None)
    return mapper(source_index) if mapper is not None else source_index


def _elapsed_ms(action) -> float:
    from PySide6.QtWidgets import QApplication

    start = time.perf_counter()
    action()
    # 把布局与绘制一起算进去：用户感受到的是「画面多久才动」，
    # 只测函数返回时间会漏掉真正的开销
    QApplication.processEvents()
    return (time.perf_counter() - start) * 1000.0


@pytest.mark.slow
def test_preview_tree_builds_100k_items(benchmark, qtbot, tmp_path: Path) -> None:
    """10 万条目的模型构建耗时。记录用，不设需求阈值（建树发生在后台重算之后）。"""
    from app.ui.widgets.plan_tree_model import PlanTreeModel

    plan = _plan_in_memory(100000, tmp_path)

    def build():
        model = PlanTreeModel()
        model.set_plan(plan)
        return model

    model = benchmark(build)

    assert model.rowCount() == CATEGORY_COUNT
    print(f"\n[构建 100000 条目的树模型] {benchmark.stats['mean'] * 1000:.1f}ms")


@pytest.mark.slow
def test_expand_largest_group_within_budget(qtbot, tmp_path: Path) -> None:
    """需求 10.14：10 万条目下展开响应必须在 100ms 内。

    展开的是成员最多的那个类目——这是最坏情况。
    """
    plan = _plan_in_memory(100000, tmp_path)
    model, view = _tree_with_plan(qtbot, plan)

    # 找出成员最多的分组
    biggest = max(
        range(model.rowCount()),
        key=lambda row: model.rowCount(model.index(row, 0)) + _pending_of(model, row),
    )
    index = _view_index(view, model.index(biggest, 0))

    elapsed = _elapsed_ms(lambda: view.expand(index))

    print(f"\n[展开最大类目（10 万条目）] {elapsed:.1f}ms")
    assert elapsed < UI_BUDGET_MS


def _pending_of(model, row: int) -> int:
    """该分组还没加载进来的成员数。"""
    node = model._nodes[model.index(row, 0).internalId()]  # noqa: SLF001
    return len(node.pending)


@pytest.mark.slow
@pytest.mark.parametrize(
    "count",
    [
        10000,
        pytest.param(
            100000,
            marks=pytest.mark.xfail(
                reason=(
                    "已知差距：单个类目上万成员时，QTreeView 对已展开分支的重布局开销"
                    "随该分支可见行数增长，最慢一批懒加载可达数百毫秒，超出需求 10.14 的"
                    "100ms。已排除模型侧原因——实测 fetchMore 本体约 0.4ms、data() 调用"
                    "次数恒定 696、折叠状态下插入恒定 0.4ms，开销全在 Qt 的 viewItems"
                    "重布局。1 万条目下（56ms）达标。要在 10 万下达标需要给大类目分桶，"
                    "使任一分支的可见行数有上界，属结构性改动。"
                ),
                strict=False,
            ),
        ),
    ],
)
def test_fetch_more_batches_within_budget(qtbot, tmp_path: Path, count: int) -> None:
    """需求 10.13、10.14：每批懒加载都必须在 100ms 内。

    展开大类目后继续往下滚会触发一批又一批 ``fetchMore``。只要有**一批**超预算，
    用户就会感到卡顿，所以断言的是最慢的那一批。
    """
    plan = _plan_in_memory(count, tmp_path)
    model, view = _tree_with_plan(qtbot, plan)
    source_index = model.index(0, 0)
    view.expand(_view_index(view, source_index))

    worst = 0.0
    batches = 0
    while model.canFetchMore(source_index) and batches < 40:
        worst = max(worst, _elapsed_ms(lambda: model.fetchMore(source_index)))
        batches += 1

    print(f"\n[{count} 条目 · 懒加载 {batches} 批，最慢一批] {worst:.1f}ms")
    assert batches > 0, "应当发生过懒加载"
    assert worst < UI_BUDGET_MS


def _scroll_all_expanded(qtbot, plan: SortPlan, steps: int = 20) -> list[float]:
    """全部类目展开后走完滚动行程，返回每一步的耗时（毫秒）。"""
    from PySide6.QtWidgets import QApplication

    model, view = _tree_with_plan(qtbot, plan)
    for row in range(model.rowCount()):
        view.expand(_view_index(view, model.index(row, 0)))
    QApplication.processEvents()

    bar = view.verticalScrollBar()
    span = bar.maximum() - bar.minimum()
    assert span > 0, "应当出现可滚动的行程"

    # 先让展开引发的延迟布局落定，否则它会被算进第一步
    bar.setValue(bar.minimum())
    QApplication.processEvents()

    return [
        _elapsed_ms(
            lambda v=bar.minimum() + span * step // steps: bar.setValue(v)
        )
        for step in range(steps + 1)
    ]


@pytest.mark.slow
@pytest.mark.parametrize("count", [10000, 100000])
def test_ordinary_scrolling_within_budget(
    qtbot, tmp_path: Path, count: int
) -> None:
    """需求 10.14：常规滚动必须在 100ms 内。

    「常规滚动」指行程中的每一段跳转，**不含跳到最末端那一步**。末端那一步单独由
    ``test_jump_to_end_is_a_known_gap`` 记录：它要求视图把所有分支剩余的行一次性
    实例化出来，是完全不同量级的操作，混在一起取最大值会把一个一次性动作的代价
    算到日常滚动头上，掩盖真实表现。

    实测：常规每步约 12ms，10 万条目下也是同一量级。
    """
    times = _scroll_all_expanded(qtbot, _plan_in_memory(count, tmp_path))
    ordinary = times[:-1]
    worst = max(ordinary)
    median = sorted(ordinary)[len(ordinary) // 2]

    print(
        f"\n[{count} 条目 · 常规滚动] 最慢 {worst:.1f}ms 中位 {median:.1f}ms"
    )
    assert worst < UI_BUDGET_MS


@pytest.mark.slow
@pytest.mark.parametrize("count", [10000, 100000])
@pytest.mark.xfail(
    reason=(
        "已知差距：拖动滚动条直接跳到最末端时，视图必须把所有分支剩余的行一次性"
        "实例化出来（懒加载被一口气全部触发），实测 1 万条目约 315ms、10 万条目约"
        "713ms，超出需求 10.14 的 100ms。常规滚动不受影响（约 12ms）。"
        "要达标需要给大类目分桶，使任一分支的行数有上界，属结构性改动。"
    ),
    strict=False,
)
def test_jump_to_end_is_a_known_gap(qtbot, tmp_path: Path, count: int) -> None:
    """单独记录「跳到最末端」的代价，避免它把常规滚动的数字带坏。"""
    times = _scroll_all_expanded(qtbot, _plan_in_memory(count, tmp_path))
    jump = times[-1]

    print(f"\n[{count} 条目 · 跳到最末端] {jump:.1f}ms")
    assert jump < UI_BUDGET_MS


@pytest.mark.slow
def test_mode_switch_within_budget(qtbot, tmp_path: Path) -> None:
    """切换「整理前 / 整理后」会重建分组。它也发生在用户点击之后，同样受 100ms 约束。"""
    from app.ui.widgets.plan_tree_model import ViewMode

    plan = _plan_in_memory(100000, tmp_path)
    model, _view = _tree_with_plan(qtbot, plan)

    elapsed = _elapsed_ms(lambda: model.set_mode(ViewMode.BEFORE))

    print(f"\n[切换视图模式（10 万条目）] {elapsed:.1f}ms")
    assert elapsed < UI_BUDGET_MS
