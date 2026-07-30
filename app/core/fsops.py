"""文件原语：同卷判定、原子重命名、复制校验、回收站、长路径与命名工具。

这个模块承载「移动过程在物理层面就不可能丢数据」这条承诺（需求 12），因此每个
函数都刻意做得很窄，便于被穷举测试（属性 25、26、27、39）。

## 为什么不用 shutil.move

``shutil.move`` 跨卷时是「先复制后 unlink」：源文件被永久删除而不是进回收站，
且校验失败时也无法保证源仍在。需求 12.2/12.3 要求跨卷路径必须「复制 → 校验
大小与哈希 → 源文件进回收站」，校验不一致时删掉目标副本并保留源文件。所以这里
自己实现 ``copy_verify_trash``。

## 删目标为什么用 unlink 而不是回收站

``copy_verify_trash`` 校验失败时删除的是本次刚复制出来的不完整副本，不是用户
数据，直接 ``unlink`` 是对的（需求 12.3）。真正属于用户的文件一律走 ``to_trash``。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# 回收站函数的类型。属性测试用 TrashRecorder 替换它来断言 trashed 记录集合。
TrashFn = Callable[[Path], None]

# ---------------------------------------------------------------------------
# Windows 路径与命名约束
# ---------------------------------------------------------------------------

# 需求 9.7：目标绝对路径超过 259 个字符即标记 path_too_long。
# 259 而非 260：MAX_PATH 是 260 且包含结尾的 NUL。
MAX_PATH_LEN = 259

# 需求 9.8：类目名中的这些字符替换为下划线。
_ILLEGAL_CHARS = r'\/:*?"<>|'
_ILLEGAL_RE = re.compile(f"[{re.escape(_ILLEGAL_CHARS)}]")

# Windows 保留设备名，不区分大小写，且带扩展名同样被保留（CON.txt 也不行）
_RESERVED_NAMES = frozenset(
    {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

# 自动重命名的形态：`文件 (2).pdf`
_SUFFIX_RE = re.compile(r"^(?P<stem>.*?)(?: \((?P<n>\d+)\))?$")


def sanitize_segment(segment: str) -> str:
    """清洗单个路径段，使其可作为 Windows 目录名或文件名。需求 9.8。

    只作用于**单个段**——这正是类目路径用 ``tuple[str, ...]`` 而非拼接字符串的
    原因：段内的 "/" 是需要被清洗的非法字符，段之间的 "/" 是层级分隔。如果先拼
    成字符串再清洗，就分不清这两者了。
    """
    cleaned = _ILLEGAL_RE.sub("_", segment)
    # 去掉控制字符
    cleaned = "".join(ch if ch >= " " else "_" for ch in cleaned)
    # Windows 会静默丢弃结尾的点与空格，导致「写入的名字」与「实际的名字」不一致
    cleaned = cleaned.rstrip(". ")
    if not cleaned:
        return "_"
    if cleaned.split(".", 1)[0].upper() in _RESERVED_NAMES:
        cleaned = "_" + cleaned
    return cleaned


def sanitize_parts(parts: tuple[str, ...]) -> tuple[str, ...]:
    """逐段清洗类目路径，丢弃清洗后为空的段。"""
    return tuple(sanitize_segment(p) for p in parts if p)


def is_path_too_long(path: Path | str) -> bool:
    """需求 9.7。"""
    return len(str(path)) > MAX_PATH_LEN


def next_available_name(target: Path, taken: frozenset[Path] | None = None) -> Path:
    """为已存在的目标生成不冲突的名称：``文件 (2).pdf``、``文件 (3).pdf`` …

    需求 9.4。``taken`` 用于在方案生成阶段避让同一批次内尚未落盘、但已被其他
    条目占用的目标名——只看磁盘会让同批两个文件算出同一个目标。
    """
    occupied = taken or frozenset()

    def is_free(candidate: Path) -> bool:
        return candidate not in occupied and not candidate.exists()

    if is_free(target):
        return target

    stem = target.stem
    suffix = target.suffix
    match = _SUFFIX_RE.match(stem)
    base = match.group("stem") if match else stem
    start = int(match.group("n")) + 1 if match and match.group("n") else 2

    counter = start
    while True:
        candidate = target.with_name(f"{base} ({counter}){suffix}")
        if is_free(candidate):
            return candidate
        counter += 1


# ---------------------------------------------------------------------------
# 卷与哈希
# ---------------------------------------------------------------------------


def _existing_ancestor(path: Path) -> Path:
    """向上找到第一个已存在的祖先。

    目标目录往往还没建出来，直接 stat 目标会失败，但它未来所在的卷由最近的已存在
    祖先决定。
    """
    current = path
    while True:
        if current.exists():
            return current
        parent = current.parent
        if parent == current:
            return current
        current = parent


def same_volume(a: Path, b: Path) -> bool:
    """两个路径是否位于同一卷。比较 ``os.stat().st_dev``。

    判定源集中在这一个函数里，属性测试用 ``VolumeStub`` 替换它即可覆盖跨卷分支，
    不必依赖机器上真有第二个卷。
    """
    try:
        dev_a = os.stat(_existing_ancestor(a)).st_dev
        dev_b = os.stat(_existing_ancestor(b)).st_dev
    except OSError:
        return False
    return dev_a == dev_b


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    """流式计算 sha256，避免把大文件整体读进内存。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 目录
# ---------------------------------------------------------------------------


def is_effectively_empty(directory: Path) -> bool:
    """需求 20.11 的唯一口径：不含任何文件且不含任何子目录。

    刻意不维护「可忽略文件」白名单。需求 20.12 要求仅含 ``desktop.ini`` 或
    ``Thumbs.db`` 的目录视为**非空**，单一口径的实现天然满足；白名单方案反而会
    违反它。
    """
    try:
        with os.scandir(directory) as it:
            return next(it, None) is None
    except OSError:
        # 读不到内容时按非空处理：宁可留下目录，也不能误删
        return False


def is_predicted_empty(dir_listing: frozenset[Path], moved_out: frozenset[Path]) -> bool:
    """纯函数：用当前目录内容减去本次将移出的条目后是否为空。

    模拟运行的待删清单与真实执行的实际删除共用这一个判定（属性 38），因此预测与
    实际不可能出现偏差。
    """
    return not (dir_listing - moved_out)


def ensure_dir(directory: Path) -> bool:
    """确保目录存在，返回「本次是否新建」。

    返回值用于决定要不要写 ``created_dir`` 记录（需求 12.5）——只有我们自己建的
    目录才允许在撤销时删掉。
    """
    if directory.is_dir():
        return False
    directory.mkdir(parents=True, exist_ok=True)
    return True


def created_dir_chain(directory: Path, stop_at: Path) -> tuple[Path, ...]:
    """``directory`` 为建成而需要新建的各级目录，从浅到深。

    多级类目（``文档/PDF/2024``）一次 mkdir 可能创建三层，撤销时这三层都要被
    当作 ``created_dir`` 逐级回收，否则会留下空壳目录。
    """
    pending: list[Path] = []
    current = directory
    while not current.exists() and current != stop_at and current != current.parent:
        pending.append(current)
        current = current.parent
    return tuple(reversed(pending))


# ---------------------------------------------------------------------------
# 移动
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MoveOutcome:
    """一次移动的结果。

    ``ok`` 为 false 时，源文件必定仍在原路径，且不存在残留的目标副本——这是
    属性 26（中断与失败时文件不丢）的实现侧保证。
    """

    ok: bool
    size: int | None = None
    sha256: str | None = None
    error: str | None = None
    trashed_source: bool = False

    @classmethod
    def success(
        cls,
        size: int,
        digest: str | None = None,
        trashed_source: bool = False,
    ) -> MoveOutcome:
        return cls(ok=True, size=size, sha256=digest, trashed_source=trashed_source)

    @classmethod
    def failure(cls, error: str) -> MoveOutcome:
        return cls(ok=False, error=error)


def atomic_move(src: Path, dst: Path) -> None:
    """同卷移动：``os.replace`` 原子重命名。需求 12.1。

    这是纯元数据操作，不复制数据、不改内容，因此物理上不可能丢数据，撤销就是
    反向 rename。
    """
    os.replace(src, dst)


def copy_verify_trash(
    src: Path,
    dst: Path,
    verify_hash: bool = True,
    trash: TrashFn | None = None,
) -> MoveOutcome:
    """跨卷移动：复制 → 校验大小与哈希 → 源文件进回收站。需求 12.2、12.3。

    校验不一致时删除已复制的目标副本、保留源文件、返回失败。删目标用 ``unlink``
    而非回收站：那份副本是本次刚产生的不完整数据，不是用户文件。
    """
    trash_fn = trash or to_trash
    try:
        src_size = src.stat().st_size
        src_digest = sha256_of(src) if verify_hash else None
    except OSError as exc:
        return MoveOutcome.failure(f"读取源文件失败: {exc}")

    try:
        shutil.copy2(src, dst)
    except OSError as exc:
        _discard_partial(dst)
        return MoveOutcome.failure(f"复制到目标失败: {exc}")

    try:
        dst_size = dst.stat().st_size
        dst_digest = sha256_of(dst) if verify_hash else None
    except OSError as exc:
        _discard_partial(dst)
        return MoveOutcome.failure(f"校验目标文件失败: {exc}")

    if dst_size != src_size or (verify_hash and dst_digest != src_digest):
        _discard_partial(dst)
        return MoveOutcome.failure(
            f"跨卷复制校验不一致：源 {src_size} 字节，目标 {dst_size} 字节"
        )

    try:
        trash_fn(src)
    except OSError as exc:
        # 目标已校验通过，源却没能进回收站。此时两份都在，报失败让用户处置，
        # 但不删任何一份——删哪份都有丢数据的风险。
        return MoveOutcome.failure(f"源文件未能移入回收站: {exc}")

    return MoveOutcome.success(src_size, dst_digest, trashed_source=True)


def _discard_partial(path: Path) -> None:
    """丢弃本次产生的不完整目标副本，失败不抛。"""
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 回收站
# ---------------------------------------------------------------------------

def to_trash(path: Path) -> None:
    """把文件或目录移入 Windows 回收站。需求 1.7。

    全应用只有这一个删除用户数据的出口，且它不做不可逆删除。属性测试用
    ``TrashRecorder`` 替换本函数来断言 ``trashed`` 记录集合（属性 27）。
    """
    from send2trash import send2trash  # 延迟导入，使 core 的纯逻辑测试无需该依赖

    send2trash(os.fspath(path))
