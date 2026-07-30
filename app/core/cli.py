"""无界面入口：在没有 Qt 的环境里跑通整条规划链。

    python -m app.core.cli --root D:\\下载 --strategy by_type --dry-run

它同时是「core 层零 Qt 依赖」这条边界的可执行证据（需求 18.7）：如果哪天有人在
core 里 import 了 PySide6，这条命令会立刻失败——而分层守卫测试只能查静态 import，
查不出运行期的动态导入。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from app.core.models import (
    ActionKind,
    ClassifyOptions,
    ConflictKind,
    ConflictPolicy,
    DateGranularity,
    ScanOptions,
    Strategy,
)
from app.core.planner import Planner
from app.core.rules import RuleEngine, RuleSerializer
from app.core.safety import SafetyGuard
from app.core.scanner import ScanSession


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.core.cli",
        description="DocSorter 无界面规划器：扫描目录并打印分类方案，不改动任何文件。",
    )
    parser.add_argument("--root", required=True, help="要整理的目录")
    parser.add_argument(
        "--strategy",
        default=Strategy.BY_TYPE.value,
        choices=[s.value for s in Strategy],
        help="分类策略",
    )
    parser.add_argument(
        "--granularity",
        default=DateGranularity.YEAR_MONTH.value,
        choices=[g.value for g in DateGranularity],
        help="时间类目粒度",
    )
    parser.add_argument(
        "--conflict",
        default=ConflictPolicy.AUTO_RENAME.value,
        choices=[p.value for p in ConflictPolicy],
        help="同名冲突策略",
    )
    parser.add_argument("--include-hidden", action="store_true", help="包含隐藏文件")
    parser.add_argument(
        "--select",
        action="append",
        default=[],
        metavar="子文件夹",
        help="让某个子文件夹的散落文件也参与整理，可重复指定",
    )
    parser.add_argument("--rules", help="自定义规则文件路径")
    parser.add_argument(
        "--min-confidence", type=float, default=0.6, help="采纳建议的置信度阈值"
    )
    parser.add_argument(
        "--merge-small",
        action="store_true",
        help="把文件数少于阈值的类目并入「其他」",
    )
    parser.add_argument("--small-threshold", type=int, default=3)
    parser.add_argument("--limit", type=int, default=25, help="每个类目最多列出几条")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="占位参数：本命令**从不**改动文件，加不加都一样",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    guard = SafetyGuard()
    root = Path(args.root)
    admission = guard.check_root(root)
    if not admission.allowed:
        print(f"[拒绝] {admission.reason}: {admission.message}", file=sys.stderr)
        return 2
    root = admission.resolved or root

    engine, rule_errors = _load_engine(args.rules)
    for error in rule_errors:
        print(f"[规则] {error.describe()}", file=sys.stderr)
    if rule_errors:
        print("[规则] 已回落到内置默认规则", file=sys.stderr)

    session = ScanSession(
        root,
        options=ScanOptions(include_hidden=args.include_hidden),
        guard=guard,
    )
    result = session.initial_scan()

    print(f"根目录        {root}")
    print(f"散落文件      {len(result.entries)}")
    print(f"子文件夹      {len(result.subfolders)}")
    if result.errors:
        print(f"读取失败      {len(result.errors)}")

    if result.subfolders:
        print("\n子文件夹清单（用 --select 名称 让其中的散落文件也参与整理）")
        for info in result.subfolders:
            print(
                f"  {info.name:<28} 散落 {info.loose_file_count:>6}"
                f"   全部 {info.recursive_file_count:>7}"
            )

    for name in args.select:
        candidate = Path(name)
        folder = candidate if candidate.is_absolute() else root / name
        delta = session.select(folder)
        status = "已勾选" if delta.added or folder in session.selection().selected else "未生效"
        print(f"\n[勾选] {folder}  {status}，新增 {len(delta.added)} 个文件")

    entries = session.entries()
    if not entries:
        print("\n扫描范围内没有可整理的文件。")
        return 0

    planner = Planner(engine=engine, guard=guard)
    plan_result = planner.build(
        entries,
        root=root,
        strategy=Strategy(args.strategy),
        options=ClassifyOptions(
            strategy=Strategy(args.strategy),
            min_confidence=args.min_confidence,
            date_granularity=DateGranularity(args.granularity),
            merge_small_categories=args.merge_small,
            small_category_threshold=args.small_threshold,
        ),
        conflict_policy=ConflictPolicy(args.conflict),
        selection=session.selection(),
        # 逐个文件试 O_RDWR 在大目录上很慢，而 CLI 只用于查看方案、不执行
        check_locked=False,
    )
    _print_plan(plan_result, limit=args.limit)
    return 0


def _load_engine(path: str | None) -> tuple[RuleEngine, list]:
    if not path:
        return RuleEngine(), []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[规则] 无法读取 {path}: {exc}", file=sys.stderr)
        return RuleEngine(), []
    rules, errors = RuleSerializer.load(text)
    if errors:
        return RuleEngine(), errors
    return RuleEngine(rules), []


def _print_plan(plan_result: object, limit: int) -> None:
    plan = plan_result.plan  # type: ignore[attr-defined]
    space = plan_result.space  # type: ignore[attr-defined]
    stats = plan.stats()

    print("\n=== 分类方案 ===")
    print(f"策略          {plan.strategy}")
    print(f"文件总数      {stats.total_files}")
    print(f"类目数        {stats.category_count}")
    print(f"待移动        {stats.pending_move}")
    print(f"冲突          {stats.conflict_count}")
    print(f"未分类        {stats.unclassified_count}")

    if not space.ok:
        print(f"\n[阻止执行] {space.message}")

    conflicts = Counter(
        item.conflict for item in plan.all_items() if item.conflict is not ConflictKind.NONE
    )
    if conflicts:
        print("\n冲突分布")
        for kind, count in conflicts.most_common():
            print(f"  {kind:<16} {count}")

    by_category: dict[str, list] = {}
    for item in plan.all_items():
        category = plan.category_by_id(item.category_id)
        label = "/".join(category.path_parts) if category else "?"
        by_category.setdefault(label, []).append(item)

    print("\n目标结构")
    for label in sorted(by_category):
        items = by_category[label]
        print(f"\n  {label}/   （{len(items)} 个文件）")
        for item in items[:limit]:
            marks = []
            if item.action is ActionKind.SKIP:
                marks.append("跳过")
            if item.conflict is not ConflictKind.NONE:
                marks.append(str(item.conflict))
            if item.renamed_from:
                marks.append(f"{item.renamed_from} → {item.target.name}")
            if not item.included:
                marks.append("未勾选")
            suffix = f"   [{', '.join(marks)}]" if marks else ""
            print(f"    {item.entry.name}{suffix}")
            print(f"        {item.reason}")
        if len(items) > limit:
            print(f"    …… 另有 {len(items) - limit} 个")


if __name__ == "__main__":
    raise SystemExit(main())
