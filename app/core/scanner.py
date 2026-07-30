"""Scanner 与 ScanSession：扫描范围控制与增量维护。

这个模块落地的是「目录结构默认不被改动」这条产品红线的第一环：**默认只收集根
目录下的孤立文件**，子文件夹自身及其内部内容一律不进 ``FileEntry`` 集合，除非
用户逐个勾选（需求 2.2、2.3、2.7）。

## 三个关键结构

``_listing_cache``  目录 -> 该目录下的孤立文件。**只增不删**，即使用户取消勾选也
                    保留。这样再次勾选同一目录不必重新读盘（属性 4 明确要求
                    「被重新读取的目录集合不包含已扫描过的目录」）。
``_children_cache`` 目录 -> 直属子文件夹清单，同样只增不删，供树形控件逐级展开。
``_selection``      当前生效的勾选集合。它决定哪些桶参与 ``entries()``。

把「读过什么」与「勾选了什么」分开，是 ``select`` / ``deselect`` 能构成往返的
前提，也让重复勾选不产生额外 I/O。

## 统计与收集分离

``SubfolderInfo`` 的 ``recursive_file_count`` / ``recursive_size`` 需要递归遍历，
但这趟遍历只累加数字，产物**不进** ``entries``（需求 2.5）。遍历用 ``os.scandir``
配合字符串路径，不构造 ``Path`` 对象，并用 ``(st_dev, st_ino)`` 访问集防止
hardlink 或 junction 导致同一目录被重复计数。

## 符号链接

``ScanOptions.follow_symlinks`` 同时控制文件与目录：为 false 时
``entry.is_file(follow_symlinks=False)`` 与 ``entry.is_dir(follow_symlinks=False)``
对符号链接都返回 False，于是链接既不进 entries 也不被递归（需求 1.6）。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from app.core.models import (
    FileEntry,
    ScanOptions,
    ScanScope,
    ScanSelection,
    SubfolderInfo,
)
from app.core.progress import (
    DEFAULT_INTERVAL_MS,
    CancelToken,
    NullCancelToken,
    ProgressSink,
    ProgressSnapshot,
    ProgressThrottle,
)
from app.core.safety import SafetyGuard

#: 每处理这么多个目录项检查一次取消，保证 500ms 内退出（需求 2.18）
CANCEL_CHECK_INTERVAL = 64

#: Windows FILE_ATTRIBUTE_HIDDEN
_FILE_ATTRIBUTE_HIDDEN = 0x2

PHASE_ENTRIES = "scan_entries"
PHASE_SUBFOLDERS = "scan_subfolders"


# ---------------------------------------------------------------------------
# 结果模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanError:
    """单个条目或目录的读取失败。

    与 ``FileEntry.error`` 双写：这里供 UI 汇总展示，那里让条目自带失败原因
    （属性 5 要求「失败条目的 error 字段非空」）。
    """

    path: Path
    stage: str  # "listdir" | "stat"
    message: str


@dataclass
class ScanResult:
    entries: list[FileEntry] = field(default_factory=list)
    subfolders: list[SubfolderInfo] = field(default_factory=list)
    errors: list[ScanError] = field(default_factory=list)
    elapsed_ms: int = 0
    cancelled: bool = False


@dataclass
class ScanDelta:
    """一次勾选变更带来的增量。

    ``cancelled`` 是在设计的 ``ScanDelta`` 之外补的字段：补扫可能被取消，而半个
    范围的方案比没有方案更危险，所以要能明确告知调用方「这次什么都没改」。
    """

    added: list[FileEntry] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)
    subfolders: list[SubfolderInfo] = field(default_factory=list)
    cancelled: bool = False


@dataclass
class _Listing:
    files: list[FileEntry] = field(default_factory=list)
    subdirs: list[Path] = field(default_factory=list)
    errors: list[ScanError] = field(default_factory=list)
    cancelled: bool = False


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _stat_of(entry: os.DirEntry[str]) -> os.stat_result | None:
    try:
        return entry.stat(follow_symlinks=False)
    except OSError:
        return None


def _is_hidden(name: str, st: os.stat_result | None) -> bool:
    """隐藏判定：Windows 隐藏属性位**或**名字以点开头。

    两者取或是必要的：Windows 靠属性位，但从 Unix 拷过来的 dotfile 不带该属性位，
    只判属性位会把它们当普通文件收进来。
    """
    if name.startswith("."):
        return True
    attrs = getattr(st, "st_file_attributes", 0) if st is not None else 0
    return bool(attrs & _FILE_ATTRIBUTE_HIDDEN)


def _split_ext(name: str) -> str:
    """扩展名，小写且含前导点；无扩展名返回空串。

    不用 ``Path.suffix``：这里只有字符串，构造 Path 在 10 万条目的量级上是可观的
    额外开销。
    """
    dot = name.rfind(".")
    if dot <= 0 or dot == len(name) - 1:
        return ""
    return name[dot:].lower()


def _identity(st: os.stat_result | None) -> tuple[int, int] | None:
    if st is None:
        return None
    if not st.st_ino:
        return None
    return (st.st_dev, st.st_ino)


# ---------------------------------------------------------------------------
# ScanSession
# ---------------------------------------------------------------------------


class ScanSession:
    """持有扫描范围状态；勾选与取消勾选只做增量。"""

    def __init__(
        self,
        root: Path,
        options: ScanOptions | None = None,
        guard: SafetyGuard | None = None,
        clock: Callable[[], float] = time.monotonic,
        progress_interval_ms: int = DEFAULT_INTERVAL_MS,
    ) -> None:
        self._root = Path(root).resolve()
        self._options = ScanOptions() if options is None else options
        self._guard = SafetyGuard() if guard is None else guard
        self._clock = clock
        self._progress_interval_ms = progress_interval_ms

        self._excluded = {name.lower() for name in self._options.excluded_names}

        self._selection = ScanSelection(root=self._root)
        self._listing_cache: dict[Path, list[FileEntry]] = {}
        self._children_cache: dict[Path, list[SubfolderInfo]] = {}
        self._read_counts: dict[Path, int] = {}
        self._errors: list[ScanError] = []

    # -- 只读访问 ---------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    @property
    def options(self) -> ScanOptions:
        return self._options

    @property
    def read_directories(self) -> frozenset[Path]:
        """曾被列举过的目录（只统计条目收集，不含递归统计那趟）。"""
        return frozenset(self._read_counts)

    def read_count(self, directory: Path) -> int:
        """某个目录被列举的次数。

        属性 4 要求「已扫描过的目录不被重新读取」——只有次数能证明这一点，
        集合无法区分「读过一次」和「读过三次」。
        """
        return self._read_counts.get(Path(directory).resolve(), 0)

    def selection(self) -> ScanSelection:
        return self._selection

    def scope(self) -> ScanScope:
        """需求 2.9。"""
        return self._selection.scope()

    def errors(self) -> list[ScanError]:
        return list(self._errors)

    def entries(self) -> list[FileEntry]:
        """当前扫描范围内的全部 FileEntry。

        = 根目录的孤立文件 + 每个已勾选子文件夹的孤立文件。顺序稳定（按路径），
        便于 UI 与快照比较。
        """
        collected: list[FileEntry] = list(self._listing_cache.get(self._root, ()))
        for folder in sorted(self._selection.selected, key=str):
            collected.extend(self._listing_cache.get(folder, ()))
        return collected

    def subfolders(self) -> list[SubfolderInfo]:
        """根目录的直属子文件夹清单，勾选状态取自当前 selection。"""
        return self._children_with_selection(self._root)

    # -- 扫描 -------------------------------------------------------------

    def initial_scan(
        self,
        cancel: CancelToken | None = None,
        on_progress: ProgressSink | None = None,
    ) -> ScanResult:
        """扫描根目录：收集孤立文件并产出子文件夹清单。需求 2.2、2.4。"""
        # 必须写成 is None：CancelToken 刻意禁止布尔求值，`cancel or ...` 会抛 TypeError
        if cancel is None:
            cancel = NullCancelToken()
        started = self._clock()

        self._selection.selected.clear()
        self._listing_cache.clear()
        self._children_cache.clear()
        self._read_counts.clear()
        self._errors.clear()

        throttle = self._make_throttle(on_progress)
        counter = _Counter()

        listing = self._list_directory(self._root, depth=1, cancel=cancel, phase=PHASE_ENTRIES, throttle=throttle, counter=counter)
        if listing.cancelled:
            return self._cancelled_result(started, listing.errors)

        self._listing_cache[self._root] = listing.files
        self._errors.extend(listing.errors)

        infos = self._describe_subfolders(
            listing.subdirs, depth=2, cancel=cancel, throttle=throttle, counter=counter
        )
        if infos is None:
            return self._cancelled_result(started, listing.errors)

        self._children_cache[self._root] = infos
        if throttle is not None:
            throttle.finish(
                ProgressSnapshot(
                    phase=PHASE_SUBFOLDERS,
                    processed=counter.value,
                    total=counter.value,
                )
            )

        return ScanResult(
            entries=list(listing.files),
            subfolders=self._children_with_selection(self._root),
            errors=list(self._errors),
            elapsed_ms=self._elapsed_ms(started),
        )

    def expand(self, folder: Path) -> list[SubfolderInfo]:
        """逐级展开：返回 ``folder`` 的直属子文件夹清单。需求 2.6。

        展开**不等于**勾选（需求 2.7）——它只是让用户看见下一层有什么。
        """
        target = self._normalize(folder)
        if target in self._children_cache:
            return self._children_with_selection(target)

        listing = self._list_directory(
            target, depth=self._depth_of(target), cancel=NullCancelToken(),
            phase=PHASE_ENTRIES, throttle=None, counter=_Counter(),
        )
        self._listing_cache.setdefault(target, listing.files)
        self._errors.extend(listing.errors)

        infos = self._describe_subfolders(
            listing.subdirs,
            depth=self._depth_of(target) + 1,
            cancel=NullCancelToken(),
            throttle=None,
            counter=_Counter(),
        )
        self._children_cache[target] = infos or []
        return self._children_with_selection(target)

    def select(
        self,
        folder: Path,
        cancel: CancelToken | None = None,
    ) -> ScanDelta:
        """勾选子文件夹：其**孤立文件**参与整理。需求 2.7、2.10。

        下级子文件夹保持未勾选状态，直到用户单独勾选。
        """
        if cancel is None:
            cancel = NullCancelToken()
        target = self._normalize(folder)

        if not self._is_selectable(target):
            return ScanDelta()
        if target in self._selection.selected:
            return ScanDelta()

        if target not in self._listing_cache:
            listing = self._list_directory(
                target, depth=self._depth_of(target), cancel=cancel,
                phase=PHASE_ENTRIES, throttle=None, counter=_Counter(),
            )
            if listing.cancelled:
                return ScanDelta(cancelled=True)
            self._listing_cache[target] = listing.files
            self._errors.extend(listing.errors)
            if target not in self._children_cache:
                infos = self._describe_subfolders(
                    listing.subdirs,
                    depth=self._depth_of(target) + 1,
                    cancel=cancel,
                    throttle=None,
                    counter=_Counter(),
                )
                if infos is None:
                    return ScanDelta(cancelled=True)
                self._children_cache[target] = infos

        self._selection.selected.add(target)
        return ScanDelta(
            added=list(self._listing_cache.get(target, ())),
            subfolders=self._children_with_selection(target),
        )

    def deselect(self, folder: Path) -> ScanDelta:
        """取消勾选：把来自该目录的孤立文件移出 FileEntry 集合。需求 2.11。

        **只作用于该目录本身，不动其下级的勾选状态。** design.md 在这里写的是
        「把 S 的下级从 selection 中一并移除」，但那与两处更高层的约定冲突：
        需求 2.7 允许「只勾子不勾父」（design.md 的生成器维度表也把这种组合列为
        必测），而属性 4 要求「先勾选 S 再取消勾选 S 之后，勾选集合回到操作前的
        状态」——若 deselect 顺带清掉下级，它就不是 select 的逆操作，这条往返
        属性在「S 的下级原本已被勾选」的场景下必然失败。故以需求与属性为准。
        """
        target = self._normalize(folder)
        if target not in self._selection.selected:
            return ScanDelta()

        self._selection.selected.discard(target)
        removed = [entry.path for entry in self._listing_cache.get(target, ())]
        return ScanDelta(removed=removed)

    def set_selected(
        self,
        folder: Path,
        selected: bool,
        cancel: CancelToken | None = None,
    ) -> ScanDelta:
        """勾选状态的统一入口，供 UI 的复选框直接绑定。"""
        return (
            self.select(folder, cancel) if selected else self.deselect(folder)
        )

    # -- 内部 -------------------------------------------------------------

    def _make_throttle(self, sink: ProgressSink | None) -> ProgressThrottle | None:
        if sink is None:
            return None
        return ProgressThrottle(
            sink, interval_ms=self._progress_interval_ms, clock=self._clock
        )

    def _elapsed_ms(self, started: float) -> int:
        return int((self._clock() - started) * 1000)

    def _cancelled_result(self, started: float, errors: Iterable[ScanError]) -> ScanResult:
        """取消即丢弃全部结果（需求 2.18）。

        半个范围的方案会让用户以为「这就是全部」，比没有方案更危险。
        """
        self._listing_cache.clear()
        self._children_cache.clear()
        self._selection.selected.clear()
        return ScanResult(
            errors=list(errors),
            elapsed_ms=self._elapsed_ms(started),
            cancelled=True,
        )

    def _normalize(self, folder: Path) -> Path:
        return Path(folder).resolve()

    def _depth_of(self, folder: Path) -> int:
        """相对根目录的层级：根目录下的孤立文件为 1，直属子文件夹内的为 2。"""
        if folder == self._root:
            return 1
        try:
            return len(folder.relative_to(self._root).parts) + 1
        except ValueError:
            return 1

    def _is_excluded(self, name: str) -> bool:
        return name.lower() in self._excluded

    def _is_selectable(self, folder: Path) -> bool:
        """只允许勾选根目录之内、真实存在的目录，且不能是根目录自身。"""
        if folder == self._root:
            return False
        if not self._guard.check_target(self._root, folder):
            return False
        if self._is_excluded(folder.name):
            return False
        return folder.is_dir()

    def _children_with_selection(self, parent: Path) -> list[SubfolderInfo]:
        infos = self._children_cache.get(parent, [])
        return [
            SubfolderInfo(
                path=info.path,
                name=info.name,
                loose_file_count=info.loose_file_count,
                recursive_file_count=info.recursive_file_count,
                recursive_size=info.recursive_size,
                depth=info.depth,
                has_children=info.has_children,
                selected=info.path in self._selection.selected,
            )
            for info in infos
        ]

    def _list_directory(
        self,
        directory: Path,
        depth: int,
        cancel: CancelToken,
        phase: str,
        throttle: ProgressThrottle | None,
        counter: _Counter,
    ) -> _Listing:
        """列举单个目录：孤立文件进 files，直属子目录进 subdirs。

        绝不递归——这是需求 2.3 的实现落点。
        """
        result = _Listing()
        follow = self._options.follow_symlinks

        try:
            iterator = os.scandir(directory)
        except OSError as exc:
            result.errors.append(ScanError(directory, "listdir", str(exc)))
            return result

        self._read_counts[directory] = self._read_counts.get(directory, 0) + 1

        with iterator:
            for entry in iterator:
                counter.value += 1
                if counter.value % CANCEL_CHECK_INTERVAL == 0 and cancel.cancelled:
                    result.cancelled = True
                    return result
                if throttle is not None:
                    throttle.update(
                        ProgressSnapshot(
                            phase=phase, processed=counter.value, current=entry.name
                        )
                    )

                if self._is_excluded(entry.name):
                    continue

                st = _stat_of(entry)
                hidden = _is_hidden(entry.name, st)
                if hidden and not self._options.include_hidden:
                    continue

                try:
                    is_dir = entry.is_dir(follow_symlinks=follow)
                    is_file = entry.is_file(follow_symlinks=follow)
                except OSError as exc:
                    result.errors.append(ScanError(Path(entry.path), "stat", str(exc)))
                    continue

                if is_dir:
                    result.subdirs.append(Path(entry.path))
                    continue
                if not is_file:
                    # 符号链接（follow_symlinks 为 false 时）、设备节点等
                    continue

                if st is None:
                    message = "无法读取文件元数据"
                    result.errors.append(ScanError(Path(entry.path), "stat", message))
                    result.files.append(
                        FileEntry(
                            path=Path(entry.path),
                            name=entry.name,
                            ext=_split_ext(entry.name),
                            size=0,
                            mtime=0.0,
                            is_hidden=hidden,
                            depth=depth,
                            error=message,
                        )
                    )
                    continue

                result.files.append(
                    FileEntry(
                        path=Path(entry.path),
                        name=entry.name,
                        ext=_split_ext(entry.name),
                        size=st.st_size,
                        mtime=st.st_mtime,
                        is_hidden=hidden,
                        depth=depth,
                    )
                )

        result.files.sort(key=lambda e: str(e.path))
        result.subdirs.sort(key=str)
        return result

    def _describe_subfolders(
        self,
        subdirs: Iterable[Path],
        depth: int,
        cancel: CancelToken,
        throttle: ProgressThrottle | None,
        counter: _Counter,
    ) -> list[SubfolderInfo] | None:
        """为每个子目录产出统计信息。取消时返回 None。"""
        infos: list[SubfolderInfo] = []
        for folder in subdirs:
            stats = self._recursive_stats(folder, cancel, throttle, counter)
            if stats is None:
                return None
            loose, total_files, total_size, has_children = stats
            infos.append(
                SubfolderInfo(
                    path=folder,
                    name=folder.name,
                    loose_file_count=loose,
                    recursive_file_count=total_files,
                    recursive_size=total_size,
                    depth=depth,
                    has_children=has_children,
                )
            )
        return infos

    def _recursive_stats(
        self,
        folder: Path,
        cancel: CancelToken,
        throttle: ProgressThrottle | None,
        counter: _Counter,
    ) -> tuple[int, int, int, bool] | None:
        """递归统计 (孤立文件数, 递归文件数, 递归总大小, 是否有子目录)。

        只累加数字，不产出 FileEntry（需求 2.5）。用字符串路径与 ``os.scandir``，
        不构造 Path，以满足 10 万条目 3 秒的预算（需求 2.15）。

        ``visited`` 用 ``(st_dev, st_ino)`` 去重：hardlink 目录或 junction 会让同
        一份内容出现在两个位置，不去重就会把它统计两遍。
        """
        loose = 0
        total_files = 0
        total_size = 0
        has_children = False
        follow = self._options.follow_symlinks
        include_hidden = self._options.include_hidden

        visited: set[tuple[int, int]] = set()
        stack: list[tuple[str, bool]] = [(str(folder), True)]

        while stack:
            current, is_top = stack.pop()
            try:
                iterator = os.scandir(current)
            except OSError as exc:
                self._errors.append(ScanError(Path(current), "listdir", str(exc)))
                continue

            with iterator:
                for entry in iterator:
                    counter.value += 1
                    if counter.value % CANCEL_CHECK_INTERVAL == 0 and cancel.cancelled:
                        return None
                    if throttle is not None:
                        throttle.update(
                            ProgressSnapshot(
                                phase=PHASE_SUBFOLDERS,
                                processed=counter.value,
                                current=entry.name,
                            )
                        )

                    if self._is_excluded(entry.name):
                        continue

                    st = _stat_of(entry)
                    if _is_hidden(entry.name, st) and not include_hidden:
                        continue

                    try:
                        is_dir = entry.is_dir(follow_symlinks=follow)
                        is_file = entry.is_file(follow_symlinks=follow)
                    except OSError as exc:
                        self._errors.append(ScanError(Path(entry.path), "stat", str(exc)))
                        continue

                    if is_dir:
                        if is_top:
                            has_children = True
                        identity = _identity(st)
                        if identity is not None:
                            if identity in visited:
                                continue
                            visited.add(identity)
                        stack.append((entry.path, False))
                        continue

                    if not is_file:
                        continue

                    total_files += 1
                    if is_top:
                        loose += 1
                    if st is not None:
                        total_size += st.st_size

        return loose, total_files, total_size, has_children


@dataclass
class _Counter:
    """跨阶段共享的已处理条目计数，供进度与取消检查点使用。"""

    value: int = 0
