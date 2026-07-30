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
