"""进度节流与取消令牌。纯 Python，零 Qt。

core 与外界只通过入参、返回值和普通 callable 交互，不出现 Qt ``Signal``。这两个
类就是那层「控制面」：``ProgressThrottle`` 把高频进度合并成低频通知，
``CancelToken`` 让长任务能在检查点及时退出。services 层负责把它们接到 Qt 信号上。

## 节流为什么不能丢计数

扫描 10 万个文件会产生 10 万次进度更新，逐次 emit 会刷爆事件循环。但节流不能
把计数一起丢掉——否则进度条停在 97% 不动，用户会以为卡死。因此设计成：每次
``update`` 携带**累计值**，节流只决定「这一刻要不要通知」，最后由 ``finish``
无条件通知一次终值（属性 42）。

关于属性 42 的一处澄清：属性正文要求「相邻两次实际发出的进度通知之间的间隔不
小于 200 毫秒」，同时要求「最终发出的累计计数等于输入事件总数」。这两条对终止
时刻是冲突的——若最后一次 update 距上次通知不足 200ms，要保住终值就必须立刻
通知。这里的处理是把终止通知与流式通知区分开：``ProgressSnapshot.final`` 为 True
的通知是终止事件，不参与 200ms 间隔约束；属性测试应只对 ``final=False`` 的通知
序列检查间隔。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

# 需求 2.13、12.7：进度通知不高于每 200 毫秒一次
DEFAULT_INTERVAL_MS = 200


@dataclass(frozen=True)
class ProgressSnapshot:
    """某一刻的进度快照。

    ``processed`` 是**累计值**而非增量，这样节流合并中间状态就不会丢计数。
    ``total`` 为 None 表示总量未知（扫描初期还没数完）。
    """

    phase: str
    processed: int
    total: int | None = None
    current: str | None = None
    final: bool = False

    @property
    def ratio(self) -> float | None:
        if not self.total:
            return None
        return min(1.0, self.processed / self.total)


ProgressSink = Callable[[ProgressSnapshot], None]


class ProgressThrottle:
    """把高频进度合并成不高于每 ``interval_ms`` 一次的通知。

    ``clock`` 可注入（属性测试用 ``FakeClock``），返回单调秒数。
    线程安全：扫描线程里的 ``ThreadPoolExecutor`` 会并发调用 ``update``。
    """

    def __init__(
        self,
        sink: ProgressSink,
        interval_ms: int = DEFAULT_INTERVAL_MS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_ms < 0:
            raise ValueError("interval_ms 不能为负")
        self._sink = sink
        self._interval = interval_ms / 1000.0
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: ProgressSnapshot | None = None
        self._latest: ProgressSnapshot | None = None
        self._last_emit_at: float | None = None
        self._emitted = 0

    @property
    def emitted_count(self) -> int:
        """已实际发出的通知数，供测试断言节流确实生效。"""
        return self._emitted

    @property
    def has_pending(self) -> bool:
        with self._lock:
            return self._pending is not None

    def update(self, snapshot: ProgressSnapshot) -> bool:
        """记录一次进度，返回本次是否真的通知了下游。

        未到间隔就只更新待发快照。因为快照携带累计值，后来的快照完全覆盖先前的，
        合并不会损失信息。
        """
        with self._lock:
            now = self._clock()
            self._latest = snapshot
            if self._last_emit_at is not None and now - self._last_emit_at < self._interval:
                self._pending = snapshot
                return False
            self._pending = None
            self._last_emit_at = now
            self._emitted += 1
            to_send = snapshot

        self._sink(to_send)
        return True

    def finish(self, snapshot: ProgressSnapshot | None = None) -> bool:
        """终止通知：无条件发出终值，不受 200ms 间隔限制。

        这是「不丢计数」的兜底，契约是「最后一条通知必定 ``final=True`` 且携带
        终值」。因此即使 pending 为空（末次 update 恰好已经发出去了），也要用最近
        一次的快照再发一条终止通知——多一条通知无害，而契约里少一个「必定」会让
        下游不得不自己判断「这是不是最后一条」。
        """
        with self._lock:
            candidate = snapshot or self._pending or self._latest
            if candidate is None:
                return False
            self._pending = None
            self._last_emit_at = self._clock()
            self._emitted += 1
            to_send = replace(candidate, final=True)

        self._sink(to_send)
        return True

    def reset(self) -> None:
        """复用同一个节流器处理下一阶段时清空状态。"""
        with self._lock:
            self._pending = None
            self._latest = None
            self._last_emit_at = None
            self._emitted = 0


class CancelToken:
    """协作式取消。

    只是一个线程安全的布尔标志——真正的及时性来自调用方在足够密的检查点上
    调用 ``cancelled``。扫描的检查点设在每 64 个目录项之间，以满足 500ms 内退出
    （需求 2.18）。
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def reset(self) -> None:
        self._event.clear()

    def raise_if_cancelled(self) -> None:
        """需要以异常中断深层递归时使用。"""
        if self.cancelled:
            raise OperationCancelled()

    def __bool__(self) -> bool:
        """``if cancel:`` 读起来像「是否已取消」，容易误解，故显式禁止。"""
        raise TypeError("请使用 token.cancelled 而不是把 CancelToken 当布尔值")


class OperationCancelled(Exception):
    """用户主动取消。不是错误，调用方应安静地收尾。"""


class NullCancelToken(CancelToken):
    """永不取消。用于不需要取消能力的调用点，免得到处判空。"""

    def cancel(self) -> None:  # pragma: no cover - 刻意空实现
        pass

    @property
    def cancelled(self) -> bool:
        return False
