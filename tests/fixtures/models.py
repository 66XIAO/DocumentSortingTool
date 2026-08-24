"""参照实现（model-based 测试用）。

这里的函数刻意写得笨但显然正确，用 ``os.walk`` / ``Path.iterdir`` 直接算答案，
不复用任何生产代码。它们的价值在于**与被测实现完全独立**——若两者算出同一个
结果，那大概率不是同一个 bug 同时出现在两处。

    naive_loose_files            对照 Scanner 只收孤立文件
    naive_recursive_stats        对照 SubfolderInfo 的三个统计值
    naive_scan                  对照属性 3（扫描输出与朴素模型一致）
    naive_expected_removed_dirs  对照属性 37（空目录删除集合精确性，M5 实现）
    naive_apply_overrides        对照属性 20、22（override 叠加，M3 实现）
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_EXCLUDED = frozenset({".docsort"})

_FILE_ATTRIBUTE_HIDDEN = 0x2


def _is_hidden(path: Path) -> bool:
    """与生产实现同一条规则：名字以点开头，或带 Windows 隐藏属性位。"""
    if path.name.startswith("."):
        return True
    try:
        attrs = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & _FILE_ATTRIBUTE_HIDDEN)


def _excluded(name: str, excluded: frozenset[str]) -> bool:
    return name.lower() in {e.lower() for e in excluded}


def naive_loose_files(
    directory: Path,
    include_hidden: bool = False,
    excluded: frozenset[str] = DEFAULT_EXCLUDED,
) -> set[Path]:
    """directory 的直接子文件集合。"""
    found: set[Path] = set()
    for child in directory.iterdir():
        if _excluded(child.name, excluded):
            continue
        if not include_hidden and _is_hidden(child):
            continue
        if child.is_file() and not child.is_symlink():
            found.add(child)
    return found


def naive_subdirs(
    directory: Path,
    include_hidden: bool = False,
    excluded: frozenset[str] = DEFAULT_EXCLUDED,
) -> set[Path]:
    found: set[Path] = set()
    for child in directory.iterdir():
        if _excluded(child.name, excluded):
            continue
        if not include_hidden and _is_hidden(child):
            continue
        if child.is_dir() and not child.is_symlink():
            found.add(child)
    return found


def naive_recursive_stats(
    directory: Path,
    include_hidden: bool = False,
    excluded: frozenset[str] = DEFAULT_EXCLUDED,
) -> tuple[int, int, int, bool]:
    """(孤立文件数, 递归文件数, 递归总大小, 是否有子目录)。"""
    loose = len(naive_loose_files(directory, include_hidden, excluded))
    has_children = bool(naive_subdirs(directory, include_hidden, excluded))

    total_files = 0
    total_size = 0
    for current, dirnames, filenames in os.walk(directory):
        here = Path(current)
        dirnames[:] = [
            d
            for d in dirnames
            if not _excluded(d, excluded)
            and (include_hidden or not _is_hidden(here / d))
        ]
        for name in filenames:
            path = here / name
            if _excluded(name, excluded):
                continue
            if not include_hidden and _is_hidden(path):
                continue
            if path.is_symlink():
                continue
            total_files += 1
            try:
                total_size += path.stat().st_size
            except OSError:
                pass
    return loose, total_files, total_size, has_children


def naive_scan(
    root: Path,
    selected: frozenset[Path] = frozenset(),
    include_hidden: bool = False,
    excluded: frozenset[str] = DEFAULT_EXCLUDED,
) -> set[Path]:
    """扫描范围内应有的 FileEntry 路径集合。对照属性 3。

    范围 = 根目录的孤立文件 + 每个已勾选目录的孤立文件。就这么简单——如果生产
    实现算出的集合与它不同，说明范围语义被写错了。
    """
    paths = naive_loose_files(root, include_hidden, excluded)
    for folder in selected:
        paths |= naive_loose_files(folder, include_hidden, excluded)
    return paths


def _ancestors_below(path: Path, root: Path) -> list[Path]:
    """path 的各级父目录中严格位于 root 之下的那些（不含 root 自身）。"""
    result: list[Path] = []
    current = path.parent
    while current != root and root in current.parents:
        result.append(current)
        current = current.parent
    return result


def naive_expected_removed_dirs(
    before_tree: set[Path],
    moved: list[tuple[Path, Path]],
    selected_closure: frozenset[Path],
    root: Path,
) -> set[Path]:
    """需求 20 那套四条件的直译。对照属性 37。

    刻意与生产代码的推导路径完全独立：生产代码先算候选集合再做一次深度降序遍历，
    这里则是「先把执行后的树整个模拟出来，再反复扫到不动点」。两条路算出同一个集合，
    才说明那不是同一个 bug 出现在两处。

    参数：
        before_tree       执行前根目录下的全部路径（含目录）
        moved             实际发生的移动，每项是 (源路径, 目标路径)
        selected_closure  用户勾选参与整理的目录闭包
        root              整理根目录

    四条件：
        (a) 位于根目录之内
        (b) 本次 run 中有文件从该目录移出
        (c) 全部文件操作结束后既不含文件也不含子目录
        (d) 不是根目录本身
    另加需求 20.5 的两层勾选：必须落在勾选闭包内。
    """
    root = Path(root)

    # 1) 把执行后的树模拟出来：移走的源消失，目标及其各级父目录出现
    after = set(before_tree)
    for src, dst in moved:
        after.discard(Path(src))
        dst = Path(dst)
        after.add(dst)
        after.update(_ancestors_below(dst, root))

    # 2) 候选：条件 (a)(b)(d) + 勾选闭包
    candidates = {Path(src).parent for src, _ in moved}
    candidates &= set(selected_closure)
    candidates.discard(root)
    candidates = {d for d in candidates if root in d.parents}

    # 3) 条件 (c)：反复扫到不动点。子目录被删掉之后父目录才可能变空，
    #    用不动点迭代表达这件事，不必自己安排删除顺序。
    removed: set[Path] = set()
    changed = True
    while changed:
        changed = False
        for directory in sorted(candidates - removed):
            children = {
                p
                for p in after
                if p.parent == directory and p not in removed
            }
            if not children:
                removed.add(directory)
                after.discard(directory)
                changed = True
    return removed
