"""五态冲突判定与目标路径分配。需求 9.3-9.9。

## 判定顺序本身是语义的一部分

    1. path_escape     目标逃出根目录（需求 1.5）
    2. path_too_long   目标绝对路径 > 259 字符（需求 9.7）
    3. locked          源文件无法以写入方式打开（需求 9.9）
    4. exists          目标已存在同名文件（需求 9.4-9.6）
    5. none

前三态与冲突策略**无关**：它们表示「这个目标路径根本不可用」，改用自动重命名或
跳过都救不了。只有 `exists` 才是「目标被占用」，才需要按策略分流。顺序写反会让
一个既超长又同名的目标被当成普通重名去改名，改完还是超长。

## 为什么 locked 用 O_RDWR 试探

需求 9.9 要判定「无法以写入方式打开」。只看只读属性位不够——文件被别的进程独占
时属性位是正常的。``os.open(path, os.O_RDWR)`` 立刻关闭是唯一可靠的探测方式，
且不改动文件内容。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from app.core import fsops
from app.core.models import ConflictKind, ConflictPolicy
from app.core.safety import SafetyGuard


def is_locked(path: Path) -> bool:
    """源文件是否无法以写入方式打开。需求 9.9。"""
    try:
        handle = os.open(path, os.O_RDWR)
    except OSError:
        return True
    else:
        os.close(handle)
        return False


@dataclass
class TargetAllocator:
    """分配目标路径并判定冲突。

    ``_taken`` 跟踪**本批已占用**的目标路径。只查磁盘是不够的：同一批里两个源文件
    可能算出同一个目标，而它们都还没落盘，磁盘上都不存在。漏掉这一层会让两个文件
    在执行时互相覆盖。
    """

    root: Path
    guard: SafetyGuard = field(default_factory=SafetyGuard)
    _taken: set[Path] = field(default_factory=set, init=False)

    def reset(self) -> None:
        self._taken.clear()

    @property
    def taken(self) -> frozenset[Path]:
        return frozenset(self._taken)

    def allocate(
        self,
        source: Path,
        category_dir: Path,
        filename: str,
        policy: ConflictPolicy,
        check_locked: bool = True,
    ) -> tuple[Path, ConflictKind, str | None]:
        """返回 (目标路径, 冲突态, 被自动重命名前的旧名)。"""
        target = category_dir / filename

        if not self.guard.check_target(self.root, target):
            return target, ConflictKind.PATH_ESCAPE, None

        if fsops.is_path_too_long(target):
            return target, ConflictKind.PATH_TOO_LONG, None

        if check_locked and is_locked(source):
            return target, ConflictKind.LOCKED, None

        occupied = target in self._taken or target.exists()
        if not occupied:
            self._taken.add(target)
            return target, ConflictKind.NONE, None

        if policy is ConflictPolicy.SKIP:
            # 不占用目标：这个条目不会被执行
            return target, ConflictKind.EXISTS, None

        if policy is ConflictPolicy.OVERWRITE:
            # 保留冲突标记，由 UI 二次确认后才允许执行（需求 9.6）
            self._taken.add(target)
            return target, ConflictKind.EXISTS, None

        renamed = fsops.next_available_name(target, taken=frozenset(self._taken))
        if fsops.is_path_too_long(renamed):
            # 改名后反而超长——如实报告，不假装解决了
            return renamed, ConflictKind.PATH_TOO_LONG, None
        self._taken.add(renamed)
        return renamed, ConflictKind.EXISTS, filename
