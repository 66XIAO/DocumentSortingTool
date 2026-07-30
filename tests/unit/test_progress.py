"""ProgressThrottle 与 CancelToken 的测试。

节流的核心承诺是「合并中间状态但不丢计数」——属性 42。这里用可注入的假时钟做
确定性断言，不依赖真实时间。
"""

from __future__ import annotations

import pytest

from app.core.progress import (
    DEFAULT_INTERVAL_MS,
    CancelToken,
    NullCancelToken,
    OperationCancelled,
    ProgressSnapshot,
    ProgressThrottle,
)


class FakeClock:
    """可手动推进的单调时钟。"""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance_ms(self, ms: float) -> None:
        self.now += ms / 1000.0


def _snap(processed: int, total: int | None = 100) -> ProgressSnapshot:
    return ProgressSnapshot(phase="scan", processed=processed, total=total)


# ---------------------------------------------------------------------------
# 节流行为
# ---------------------------------------------------------------------------


def test_first_update_emits_immediately() -> None:
    sent: list[ProgressSnapshot] = []
    clock = FakeClock()
    throttle = ProgressThrottle(sent.append, clock=clock)

    assert throttle.update(_snap(1)) is True
    assert [s.processed for s in sent] == [1]


def test_updates_within_interval_are_merged() -> None:
    sent: list[ProgressSnapshot] = []
    clock = FakeClock()
    throttle = ProgressThrottle(sent.append, clock=clock)

    throttle.update(_snap(1))
    for processed in range(2, 60):
        clock.advance_ms(1)
        assert throttle.update(_snap(processed)) is False

    assert [s.processed for s in sent] == [1]
    assert throttle.has_pending


def test_update_emits_again_after_interval() -> None:
    sent: list[ProgressSnapshot] = []
    clock = FakeClock()
    throttle = ProgressThrottle(sent.append, clock=clock)

    throttle.update(_snap(1))
    clock.advance_ms(DEFAULT_INTERVAL_MS)
    assert throttle.update(_snap(500)) is True

    assert [s.processed for s in sent] == [1, 500]


def test_streaming_emissions_are_at_least_interval_apart() -> None:
    """属性 42 的间隔部分，只针对 final=False 的流式通知。"""
    stamps: list[float] = []
    clock = FakeClock()
    throttle = ProgressThrottle(lambda _s: stamps.append(clock.now), clock=clock)

    for processed in range(1, 2001):
        clock.advance_ms(3)
        throttle.update(_snap(processed, total=2000))

    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    assert gaps, "样本太少，测不出间隔"
    assert all(gap >= DEFAULT_INTERVAL_MS / 1000.0 - 1e-9 for gap in gaps)


def test_finish_emits_final_count_even_inside_interval() -> None:
    """属性 42 的不丢计数部分：终值必须发出，即使距上次通知不足 200ms。"""
    sent: list[ProgressSnapshot] = []
    clock = FakeClock()
    throttle = ProgressThrottle(sent.append, clock=clock)

    total = 1000
    for processed in range(1, total + 1):
        clock.advance_ms(1)
        throttle.update(_snap(processed, total=total))
    throttle.finish()

    assert sent[-1].processed == total
    assert sent[-1].final is True
    assert not throttle.has_pending


def test_final_flag_separates_terminal_from_streaming() -> None:
    sent: list[ProgressSnapshot] = []
    clock = FakeClock()
    throttle = ProgressThrottle(sent.append, clock=clock)

    throttle.update(_snap(1))
    clock.advance_ms(5)
    throttle.update(_snap(2))
    throttle.finish()

    streaming = [s for s in sent if not s.final]
    terminal = [s for s in sent if s.final]
    assert [s.processed for s in streaming] == [1]
    assert [s.processed for s in terminal] == [2]


def test_finish_without_pending_does_nothing() -> None:
    sent: list[ProgressSnapshot] = []
    clock = FakeClock()
    throttle = ProgressThrottle(sent.append, clock=clock)

    assert throttle.finish() is False
    assert sent == []


def test_finish_accepts_explicit_snapshot() -> None:
    sent: list[ProgressSnapshot] = []
    throttle = ProgressThrottle(sent.append, clock=FakeClock())

    assert throttle.finish(_snap(42)) is True
    assert sent[-1].processed == 42
    assert sent[-1].final is True


def test_zero_interval_emits_every_update() -> None:
    sent: list[ProgressSnapshot] = []
    clock = FakeClock()
    throttle = ProgressThrottle(sent.append, interval_ms=0, clock=clock)

    for processed in range(1, 6):
        throttle.update(_snap(processed))

    assert [s.processed for s in sent] == [1, 2, 3, 4, 5]


def test_negative_interval_is_rejected() -> None:
    with pytest.raises(ValueError):
        ProgressThrottle(lambda _s: None, interval_ms=-1)


def test_reset_clears_state() -> None:
    sent: list[ProgressSnapshot] = []
    clock = FakeClock()
    throttle = ProgressThrottle(sent.append, clock=clock)

    throttle.update(_snap(1))
    clock.advance_ms(5)
    throttle.update(_snap(2))
    throttle.reset()

    assert not throttle.has_pending
    assert throttle.emitted_count == 0
    assert throttle.update(_snap(3)) is True


def test_emitted_count_tracks_actual_notifications() -> None:
    clock = FakeClock()
    throttle = ProgressThrottle(lambda _s: None, clock=clock)

    throttle.update(_snap(1))
    for _ in range(50):
        clock.advance_ms(1)
        throttle.update(_snap(2))
    throttle.finish()

    assert throttle.emitted_count == 2  # 一次流式 + 一次终止


# ---------------------------------------------------------------------------
# ProgressSnapshot
# ---------------------------------------------------------------------------


def test_ratio_is_none_when_total_unknown() -> None:
    assert ProgressSnapshot(phase="scan", processed=5, total=None).ratio is None
    assert ProgressSnapshot(phase="scan", processed=5, total=0).ratio is None


def test_ratio_is_clamped() -> None:
    snap = ProgressSnapshot(phase="scan", processed=150, total=100)
    assert snap.ratio == 1.0


# ---------------------------------------------------------------------------
# CancelToken
# ---------------------------------------------------------------------------


def test_cancel_token_lifecycle() -> None:
    token = CancelToken()
    assert token.cancelled is False

    token.cancel()
    assert token.cancelled is True

    token.reset()
    assert token.cancelled is False


def test_raise_if_cancelled() -> None:
    token = CancelToken()
    token.raise_if_cancelled()  # 未取消时不抛

    token.cancel()
    with pytest.raises(OperationCancelled):
        token.raise_if_cancelled()


def test_truthiness_is_rejected() -> None:
    """`if token:` 读起来像「是否已取消」，实际总为真，是个静默陷阱。"""
    token = CancelToken()
    with pytest.raises(TypeError):
        bool(token)


def test_null_token_never_cancels() -> None:
    token = NullCancelToken()
    token.cancel()
    assert token.cancelled is False
    token.raise_if_cancelled()
