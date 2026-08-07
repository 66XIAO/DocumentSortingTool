"""测试替身。对应 design.md「共用的生成器与替身」表。

替身存在的理由都是同一个：让属性测试**不依赖不可控的外部条件**——第二个物理卷、
可用的凭据管理器、真实的回收站、真实时钟。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from app.core.llm.provider import (
    ChatRequest,
    ChatResponse,
    FailureKind,
    Provider,
    ProviderError,
)


class FakeClock:
    """可手动推进的单调时钟。"""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance_ms(self, ms: float) -> None:
        self.now += ms / 1000.0

    def advance_days(self, days: float) -> None:
        self.now += days * 86400.0


class TrashRecorder:
    """替换 send2trash，记录被移入回收站的路径。

    属性 27 要求「被覆盖策略移入回收站的文件集合等于 trashed 记录集合」——只有
    记录下来才能断言。默认真的删掉文件，使调用方看到的文件系统状态与真实回收站
    行为一致。
    """

    def __init__(self, actually_remove: bool = True) -> None:
        self.trashed: list[Path] = []
        self._remove = actually_remove

    def __call__(self, path: Path) -> None:
        self.trashed.append(Path(path))
        if not self._remove:
            return
        target = Path(path)
        if target.is_dir():
            os.rmdir(target)
        else:
            os.unlink(target)

    @property
    def paths(self) -> set[Path]:
        return set(self.trashed)


class FailingTrash:
    """回收站不可用。用于验证「源未能进回收站时两份都保留」这条分支。"""

    def __call__(self, path: Path) -> None:  # noqa: ARG002
        raise OSError("回收站不可用")


class VolumeStub:
    """把指定路径前缀伪装成不同的卷。

    跨卷分支（复制 → 校验 → 源进回收站）是「移动不丢数据」承诺里最复杂的一段，
    但机器上不一定有第二个物理卷。改写 ``fsops.same_volume`` 的判定源即可覆盖。
    """

    def __init__(self, cross_volume_roots: tuple[Path, ...] = ()) -> None:
        self.cross = tuple(Path(p) for p in cross_volume_roots)

    def same_volume(self, a: Path, b: Path) -> bool:
        return self._label(a) == self._label(b)

    def _label(self, path: Path) -> str:
        for root in self.cross:
            try:
                if Path(path).resolve().is_relative_to(root.resolve()):
                    return str(root)
            except (OSError, ValueError):
                continue
        return "<default>"

    def always_cross(self, _a: Path, _b: Path) -> bool:
        """无条件判为跨卷，用于强制走复制校验路径。"""
        return False


class MemoryKeyring:
    """内存凭据后端。"""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.fail = False

    def get_password(self, service: str, user: str) -> str | None:
        if self.fail:
            raise RuntimeError("凭据后端不可用")
        return self.store.get((service, user))

    def set_password(self, service: str, user: str, password: str) -> None:
        if self.fail:
            raise RuntimeError("凭据后端不可用")
        self.store[(service, user)] = password

    def delete_password(self, service: str, user: str) -> None:
        del self.store[(service, user)]


def stub_same_volume(
    monkeypatch: object, decision: Callable[[Path, Path], bool]
) -> None:
    """把 fsops.same_volume 换成给定判定。"""
    from app.core import fsops

    monkeypatch.setattr(fsops, "same_volume", decision)  # type: ignore[attr-defined]


class FakeProvider:
    """按脚本返回结果的 Provider 替身。需求 44 测试用。

    可注入五类失败，记录调用次数与请求历史。
    """

    def __init__(
        self,
        *,
        scripted_responses: list[str] | None = None,
        fail_with: FailureKind | None = None,
        fail_message: str = "injected failure",
    ) -> None:
        self.name = "fake"
        self._scripted = list(scripted_responses or [])
        self._fail_with = fail_with
        self._fail_message = fail_message
        self.call_count = 0
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest, timeout_seconds: float) -> ChatResponse:
        self.call_count += 1
        self.requests.append(request)

        if self._fail_with is not None:
            raise ProviderError(self._fail_with, self._fail_message)

        if self._scripted:
            content = self._scripted.pop(0)
            return ChatResponse(content=content)

        return ChatResponse(content="{}")

    def test_connectivity(self, timeout_seconds: float = 10.0) -> str:
        if self._fail_with is not None:
            raise ProviderError(self._fail_with, self._fail_message)
        return "fake ok"
