"""Safety_Guard：根目录准入、目标路径越界校验、符号链接策略。

这个模块是「批量误移动」这一最大风险的第一道闸门。它只做判定、不做 I/O 改动，
因此可以在没有任何真实文件的情况下被穷举测试（属性 1、属性 2）。

三条职责：

1. ``check_root``  用户选定的整理根目录是否可用（需求 1.1-1.3）
2. ``check_target`` Planner 产出的每个目标路径是否仍在根目录之内（需求 1.4、1.5）
3. ``is_traversable`` 扫描时是否可以进入某个目录项（需求 1.6）

其中 ``check_target`` 是 ``ConflictKind.PATH_ESCAPE`` 的**唯一**来源。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.core.models import RejectReason, RootVerdict

# ---------------------------------------------------------------------------
# 系统目录黑名单
# ---------------------------------------------------------------------------

# 需求 1.3 按字面列出了 C: 盘下的这几个目录。这里优先取环境变量，取不到才回落到
# 字面值——Windows 并不一定装在 C:，硬编码 C: 会在非 C: 系统上漏掉真正的系统目录。
_FALLBACK_SYSTEM_ROOTS: tuple[str, ...] = (
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
)

_SYSTEM_ROOT_ENV_VARS: tuple[str, ...] = (
    "SystemRoot",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "ProgramW6432",
    "ProgramData",
)

# 需求 1.3 要求把任一 AppData 目录之内的路径也拒掉。判定用**路径段相等**而非
# 字符串包含，否则 D:\MyAppDataBackup 会被误拒。
_APPDATA_SEGMENT = "appdata"


def _default_system_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    for var in _SYSTEM_ROOT_ENV_VARS:
        value = os.environ.get(var)
        if value:
            roots.append(Path(value))
    for literal in _FALLBACK_SYSTEM_ROOTS:
        roots.append(Path(literal))

    unique: dict[str, Path] = {}
    for root in roots:
        unique.setdefault(_normcase(root), root)
    return tuple(unique.values())


def _normcase(path: Path) -> str:
    """Windows 路径大小写不敏感，比较前统一折叠。

    不依赖 ``PureWindowsPath`` 的比较语义，是为了让判定逻辑在任何平台上跑出
    相同结果——属性测试因此不必区分运行平台。
    """
    return os.path.normcase(str(path))


def _is_within(child: Path, parent: Path) -> bool:
    """child 是否等于 parent 或位于 parent 之内（大小写不敏感）。"""
    child_parts = tuple(os.path.normcase(p) for p in child.parts)
    parent_parts = tuple(os.path.normcase(p) for p in parent.parts)
    if len(child_parts) < len(parent_parts):
        return False
    return child_parts[: len(parent_parts)] == parent_parts


# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdmissionResult:
    """根目录准入判定。需求 1.1。

    reject 时 reason 必定非空，allow 时必定为空——这条由 ``__post_init__`` 保证，
    使属性 1 的「reject 必附带封闭集合内的原因码」在构造期就成立。
    """

    verdict: RootVerdict
    reason: RejectReason | None = None
    message: str = ""
    resolved: Path | None = None

    def __post_init__(self) -> None:
        if self.verdict is RootVerdict.REJECT and self.reason is None:
            raise ValueError("reject 必须附带原因码")
        if self.verdict is RootVerdict.ALLOW and self.reason is not None:
            raise ValueError("allow 不应附带原因码")

    @property
    def allowed(self) -> bool:
        return self.verdict is RootVerdict.ALLOW

    @classmethod
    def allow(cls, resolved: Path) -> AdmissionResult:
        return cls(verdict=RootVerdict.ALLOW, resolved=resolved)

    @classmethod
    def reject(
        cls,
        reason: RejectReason,
        message: str,
        resolved: Path | None = None,
    ) -> AdmissionResult:
        return cls(
            verdict=RootVerdict.REJECT,
            reason=reason,
            message=message,
            resolved=resolved,
        )


_REJECT_MESSAGES: dict[RejectReason, str] = {
    RejectReason.DRIVE_ROOT: "不能直接整理整个磁盘根目录，请选择其中的具体文件夹。",
    RejectReason.SYSTEM_PATH: "该目录属于系统或程序目录，整理它可能破坏系统与已安装软件。",
    RejectReason.NOT_EXISTS: "目录不存在，请确认路径是否正确。",
    RejectReason.NOT_A_DIR: "所选路径是一个文件，请选择文件夹。",
    RejectReason.NO_PERMISSION: "没有访问该目录的权限，请换一个目录或以管理员身份运行。",
}


# ---------------------------------------------------------------------------
# SafetyGuard
# ---------------------------------------------------------------------------


class SafetyGuard:
    """路径安全判定。

    ``system_roots`` 与 ``denied_segments`` 都可注入。这不只是为了「方便测试」：
    pytest 的 ``tmp_path`` 在 Windows 上位于 ``C:\\Users\\<用户>\\AppData\\Local\\Temp``
    之下，也就是说默认规则会把每个临时目录都判成系统路径。如果黑名单硬编码不可
    注入，任何需要「一个可用的根目录」的测试都无法建立立足点。
    """

    #: 默认按路径段拒绝的目录名（小写）。需求 1.3。
    DEFAULT_DENIED_SEGMENTS: tuple[str, ...] = (_APPDATA_SEGMENT,)

    def __init__(
        self,
        system_roots: tuple[Path, ...] | None = None,
        denied_segments: tuple[str, ...] | None = None,
    ) -> None:
        self._system_roots: tuple[Path, ...] = (
            system_roots if system_roots is not None else _default_system_roots()
        )
        self._denied_segments: tuple[str, ...] = tuple(
            s.lower()
            for s in (
                denied_segments
                if denied_segments is not None
                else self.DEFAULT_DENIED_SEGMENTS
            )
        )

    @property
    def system_roots(self) -> tuple[Path, ...]:
        return self._system_roots

    @property
    def denied_segments(self) -> tuple[str, ...]:
        return self._denied_segments

    # -- 根目录准入 -------------------------------------------------------

    def check_root(self, path: Path) -> AdmissionResult:
        """判定一个路径能否作为整理根目录。需求 1.1-1.3。

        判定顺序：resolve → 存在性 → 是否目录 → 是否盘符根 → 是否系统目录。
        顺序是刻意的：先把「压根不能用」的情况分辨出来，用户拿到的提示才具体。
        """
        try:
            resolved = Path(path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return AdmissionResult.reject(
                RejectReason.NOT_EXISTS, _REJECT_MESSAGES[RejectReason.NOT_EXISTS]
            )

        try:
            exists = resolved.exists()
        except OSError:
            return AdmissionResult.reject(
                RejectReason.NO_PERMISSION,
                _REJECT_MESSAGES[RejectReason.NO_PERMISSION],
                resolved,
            )

        if not exists:
            return AdmissionResult.reject(
                RejectReason.NOT_EXISTS,
                _REJECT_MESSAGES[RejectReason.NOT_EXISTS],
                resolved,
            )

        if not resolved.is_dir():
            return AdmissionResult.reject(
                RejectReason.NOT_A_DIR,
                _REJECT_MESSAGES[RejectReason.NOT_A_DIR],
                resolved,
            )

        if self.is_drive_root(resolved):
            return AdmissionResult.reject(
                RejectReason.DRIVE_ROOT,
                _REJECT_MESSAGES[RejectReason.DRIVE_ROOT],
                resolved,
            )

        if self.is_system_path(resolved):
            return AdmissionResult.reject(
                RejectReason.SYSTEM_PATH,
                _REJECT_MESSAGES[RejectReason.SYSTEM_PATH],
                resolved,
            )

        if not self._is_readable(resolved):
            return AdmissionResult.reject(
                RejectReason.NO_PERMISSION,
                _REJECT_MESSAGES[RejectReason.NO_PERMISSION],
                resolved,
            )

        return AdmissionResult.allow(resolved)

    @staticmethod
    def is_drive_root(resolved: Path) -> bool:
        """是否为盘符根（``C:\\``）或 UNC 共享根（``\\\\server\\share\\``）。需求 1.2。

        ``p == p.anchor`` 覆盖两者：anchor 对 UNC 路径是 ``\\\\server\\share\\``。
        """
        return str(resolved) == resolved.anchor

    def is_system_path(self, resolved: Path) -> bool:
        """是否落在系统目录集合或任一被拒目录段之内。需求 1.3。

        目录段判定用**相等**而非字符串包含，否则 ``D:\\MyAppDataBackup`` 会被误拒。
        """
        parts_lower = {part.lower() for part in resolved.parts}
        if parts_lower & set(self._denied_segments):
            return True
        return any(_is_within(resolved, root) for root in self._system_roots)

    @staticmethod
    def _is_readable(resolved: Path) -> bool:
        try:
            with os.scandir(resolved) as it:
                next(it, None)
        except PermissionError:
            return False
        except OSError:
            # 其余 I/O 错误留给扫描阶段逐条目记录 error（需求 2.16），
            # 不在准入阶段一票否决整个目录。
            return True
        return True

    # -- 目标路径越界 -----------------------------------------------------

    def check_target(self, root: Path, target: Path) -> bool:
        """目标路径是否仍在根目录之内。需求 1.4、1.5。

        必须在 ``resolve()`` **之后**比较，否则 ``root\\..\\..\\X`` 这类构造能绕过。
        这是 ``ConflictKind.PATH_ESCAPE`` 的唯一来源。
        """
        try:
            resolved_root = Path(root).resolve(strict=False)
            resolved_target = Path(target).resolve(strict=False)
        except (OSError, RuntimeError):
            return False
        return _is_within(resolved_target, resolved_root)

    # -- 遍历策略 ---------------------------------------------------------

    def is_traversable(
        self,
        entry: os.DirEntry[str],
        follow_symlinks: bool = False,
    ) -> bool:
        """扫描时是否可以递归进入该目录项。需求 1.6。

        ``follow_symlinks`` 为 false 时，符号链接与 junction 都不进入。junction 要
        单独判断：Windows 上 junction 不被 ``is_symlink()`` 识别，只有 3.12 引入的
        ``is_junction()`` 能认出来。漏掉它意味着扫描可能顺着 junction 走出根目录。
        """
        try:
            if not entry.is_dir(follow_symlinks=follow_symlinks):
                return False
            if follow_symlinks:
                return True
            if entry.is_symlink():
                return False
            is_junction = getattr(entry, "is_junction", None)
            if callable(is_junction) and is_junction():
                return False
        except OSError:
            return False
        return True


def describe_reason(reason: RejectReason) -> str:
    """原因码对应的面向用户的中文说明。"""
    return _REJECT_MESSAGES.get(reason, "该目录不可用。")


# 供类型标注复用，避免调用方各写一遍字面量联合
Verdict = Literal["allow", "reject"]
