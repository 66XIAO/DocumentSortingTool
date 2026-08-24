"""源侧空目录清理的唯一口径。需求 20。

## 为什么单独成一个模块

原先有两套预测实现：

    planner.EmptyDirPredictor.predict     逐个候选目录判定，**不累积**已判定会被删的子目录
    executor.predict_removed_dirs         按深度降序遍历，累积 ``gone``

两者在「子目录被移空删掉之后，父目录才跟着变空」这种嵌套情形下给出不同答案，而且
前者**少报**——确认对话框列出的目录比实际删掉的少。这正是最危险的方向：用户以为
只删两个，实际删了五个。需求 20.7 要求确认框列出完整清单，属性 38 要求预测集合等于
实际删除集合；只有让预览、模拟运行、真实执行共用同一段代码，这条等式才是结构性
成立的，而不是靠三处各自小心地写成一样。

## 判定链

    will_move(item)          哪些条目会真正把源文件搬走（须与 Executor 逐条一致）
    collect_candidates()     四条件里的 (a)(b)(d)：在根内、有文件移出、不是根本身
    predict_removed()        条件 (c) 的**预测**形态：减去将移出的条目后是否为空
    fsops.is_effectively_empty()  条件 (c) 的**实测**形态，真实执行时用

预测与实测是同一个语义的两种时态：文件还没搬走时不能问「现在空不空」，只能问
「减去本次将移出的条目后空不空」。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from app.core import fsops
from app.core.models import (
    ActionKind,
    ConflictKind,
    PlanItem,
    ScanSelection,
    SortPlan,
)

#: 这三种冲突表示目标路径根本不可用，Executor 会直接跳过，源文件不会被搬走。
#: 与 ``Executor._handle`` 的同名判定必须保持一致，否则预测就会与实际偏离。
_BLOCKING_CONFLICTS = (
    ConflictKind.PATH_ESCAPE,
    ConflictKind.PATH_TOO_LONG,
    ConflictKind.LOCKED,
)


def will_move(item: PlanItem) -> bool:
    """该条目在执行时是否会把源文件搬离原目录。

    判定逐条对齐 ``Executor._handle``：未勾选、action 为 skip、以及三种阻断性冲突
    都不会搬动源文件，因此都不会让源目录变空。
    """
    return (
        item.included
        and item.action is not ActionKind.SKIP
        and item.conflict not in _BLOCKING_CONFLICTS
    )


def moved_sources_of(plan: SortPlan) -> list[Path]:
    """方案中会被真正搬走的源文件路径。"""
    return [item.entry.path for item in plan.all_items() if will_move(item)]


def _is_inside(directory: Path, resolved_root: Path) -> bool:
    try:
        return directory.resolve().is_relative_to(resolved_root)
    except (OSError, ValueError):
        return False


def collect_candidates(
    moved_sources: Sequence[Path],
    selection: ScanSelection,
    root: Path,
) -> list[Path]:
    """空目录清理的候选集合。需求 20.6、20.8、20.9、20.10、20.14。

    纯集合运算，无 I/O。四条件里的三条在这里落地：
        (a) 位于根目录之内
        (b) 本次 run 中有文件从该目录移出 —— 传入的就是移出文件的父目录集合
        (d) 不是根目录本身

    与勾选闭包求交，保证需求 20.5 的两层勾选：没勾选过的子文件夹永远进不了候选，
    ``top_level_only`` 下勾选集合为空，候选集合因此天然为空（需求 20.10）。
    """
    candidates = {Path(p) for p in moved_sources}
    candidates &= selection.selected_closure()
    candidates.discard(Path(root))

    resolved_root = Path(root).resolve()
    inside = {d for d in candidates if _is_inside(d, resolved_root)}
    # 深度降序：先删子目录，父目录才有机会随之变空
    return sorted(inside, key=lambda p: (len(p.parts), str(p)), reverse=True)


def predict_removed(
    candidates: Sequence[Path], moved_files: Sequence[Path]
) -> list[Path]:
    """待删空目录清单。需求 20.7、20.20。

    ``candidates`` 必须是深度降序（``collect_candidates`` 的输出即是）。
    ``gone`` 累积已判定为会被删掉的子目录，使「子目录被删后父目录才变空」这种嵌套
    情形的预测与真实执行一致——真实执行正是按同一顺序逐级删的。
    """
    moved_out = frozenset(Path(p) for p in moved_files)
    predicted: list[Path] = []
    gone: set[Path] = set()
    for directory in candidates:
        try:
            listing = frozenset(directory.iterdir())
        except OSError:
            continue
        if fsops.is_predicted_empty(listing, moved_out | frozenset(gone)):
            predicted.append(directory)
            gone.add(directory)
    return predicted


def predict_for_plan(
    plan: SortPlan, selection: ScanSelection | None
) -> list[Path]:
    """执行前的待删空目录清单。需求 20.7。

    这是 UI 确认对话框的唯一数据来源。它与 ``Executor`` 的模拟运行走同一条判定链，
    因此确认框里列出的清单就是真实执行会删掉的那些目录（属性 38）。

    ``selection`` 为 None 表示没有勾选信息，此时不可能删除任何目录（需求 20.5）。
    """
    if selection is None:
        return []
    moved = moved_sources_of(plan)
    candidates = collect_candidates(
        [p.parent for p in moved], selection, plan.root
    )
    return predict_removed(candidates, moved)
