# Feature: document-sorting-tool, Property 30: 配置序列化往返一致性
"""属性 30 与属性 42。

- 属性 30：任意合法配置对象写入 settings.yaml 后读回等价（比较范围排除 keyring
  引用字段），其中 scan.scope 与 cleanup.remove_empty_dirs 被保留。
- 属性 42：相邻两次**流式**进度通知间隔不小于 200ms，且最终发出的累计计数等于
  输入事件总数。

Validates: Requirements 18.3, 18.8, 2.13, 12.7
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.config.settings import Settings, SettingsManager
from app.core.models import (
    ActionKind,
    ConflictPolicy,
    DateGranularity,
    PrivacyLevel,
    ProviderKind,
    ScanScope,
    Strategy,
)
from app.core.progress import DEFAULT_INTERVAL_MS, ProgressSnapshot, ProgressThrottle
from tests.fixtures.doubles import FakeClock, MemoryKeyring

FS = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

#: recent_roots / dry_run_completed_roots 存的是文件系统路径。
#
# 刻意**不用** st.text()：YAML 规范把 U+0085(NEL)、U+2028(LS)、U+2029(PS) 当作
# 换行字符，含它们的字符串写入 YAML 再读回会变成空格——这是 YAML 的限制，不是本
# 项目的缺陷（见下方 test_yaml_cannot_round_trip_unicode_line_breaks 显式记录）。
# 真实路径不含这类字符，所以生成器按真实路径的字符集取值。
path_like = st.text(
    alphabet=st.sampled_from(
        "abcdefgXYZ0189中文下载资料\\/:. -_()"
    ),
    max_size=24,
)

#: YAML 无法往返的三个字符。规范把它们当换行，写入后读回会变成普通空格。
YAML_LINE_BREAKS = "\u0085\u2028\u2029"

#: 自由文本字段（base_url / host / model / api_key_ref）的生成器。
#
# 与 path_like 同源的理由：这三个字符是 **YAML 的限制**，不是本项目的缺陷（见
# test_yaml_cannot_round_trip_unicode_line_breaks 显式记录）。把它们喂给往返属性只会
# 得到一条「YAML 不能存任意 Unicode」的结论，而那不是属性 30 想说的事。真实的 URL、
# 模型名与 keyring 引用键都不含它们。
yaml_safe_text = st.text(max_size=30).filter(
    lambda value: not any(ch in value for ch in YAML_LINE_BREAKS)
)


@st.composite
def settings_objects(draw: st.DrawFn) -> Settings:
    """只生成**合法**配置：属性 30 说的是「任意合法配置对象」的往返。

    越界取值的行为由属性 10（破坏容错）负责，两者不该混在一条属性里——否则
    「往返后不相等」到底是往返坏了还是夹紧生效了就分不清。
    """
    s = Settings()
    s.scan.scope = draw(st.sampled_from(list(ScanScope)))
    s.scan.include_hidden = draw(st.booleans())
    s.scan.follow_symlinks = draw(st.booleans())

    s.classify.strategy = draw(st.sampled_from(list(Strategy)))
    s.classify.min_confidence = draw(
        st.floats(min_value=0.0, max_value=1.0, allow_nan=False, width=32)
    )
    s.classify.merge_small_categories = draw(st.booleans())
    s.classify.small_category_threshold = draw(st.integers(1, 1000))

    s.date.granularity = draw(st.sampled_from(list(DateGranularity)))

    s.conflict.policy = draw(st.sampled_from(list(ConflictPolicy)))
    s.conflict.default_action = draw(
        st.sampled_from([ActionKind.MOVE, ActionKind.COPY])
    )
    s.conflict.verify_hash = draw(st.booleans())

    s.cleanup.remove_empty_dirs = draw(st.booleans())

    s.ai.enabled = draw(st.booleans())
    s.ai.provider = draw(st.sampled_from(list(ProviderKind)))
    s.ai.base_url = draw(yaml_safe_text)
    s.ai.host = draw(yaml_safe_text)
    s.ai.model = draw(yaml_safe_text)
    s.ai.timeout_seconds = draw(st.integers(1, 600))
    s.ai.privacy_level = draw(st.sampled_from(list(PrivacyLevel)))
    s.ai.api_key_ref = draw(yaml_safe_text)
    s.ai.taxonomy_sample_size = draw(st.integers(1, 300))
    s.ai.max_categories = draw(st.integers(1, 12))
    s.ai.max_depth = draw(st.integers(1, 2))
    s.ai.batch_size = draw(st.integers(80, 120))

    s.history.max_runs = draw(st.integers(1, 1000))
    s.history.max_days = draw(st.integers(1, 3650))

    s.ui.recent_roots = tuple(draw(st.lists(path_like, max_size=4)))
    s.ui.dry_run_completed_roots = tuple(draw(st.lists(path_like, max_size=4)))
    return s


@FS
@given(original=settings_objects())
def test_settings_round_trip(tmp_path_factory, original: Settings) -> None:
    base: Path = tmp_path_factory.mktemp("cfg")
    manager = SettingsManager(base_dir=base, keyring_backend=MemoryKeyring())

    manager.save(original)
    restored = manager.load()

    assert restored == original
    assert manager.warnings == []


@FS
@given(original=settings_objects())
def test_two_required_fields_survive(tmp_path_factory, original: Settings) -> None:
    """需求 18.8 点名了这两个字段。"""
    base: Path = tmp_path_factory.mktemp("cfg")
    manager = SettingsManager(base_dir=base, keyring_backend=MemoryKeyring())

    manager.save(original)
    restored = manager.load()

    assert restored.scan.scope is original.scan.scope
    assert restored.cleanup.remove_empty_dirs is original.cleanup.remove_empty_dirs


#: 带可辨识前缀的密钥。
#
# 前缀不可省：密钥若与配置里别的字段（比如 recent_roots 的某一项）取值相同，
# 「密钥没出现在文件里」就会因为一次巧合而失败——那不是泄露，是断言写得不够精确。
# "sk-live-" 用的字符不在 path_like 的字符池里，因此不可能由那些字段产生。
api_keys = st.text(min_size=1, max_size=32).map(lambda body: f"sk-live-{body}")


@FS
@given(original=settings_objects(), secret=api_keys)
def test_api_key_plaintext_never_reaches_the_file(
    tmp_path_factory, original: Settings, secret: str
) -> None:
    """需求 7.3：文件里只有 keyring 引用键。"""
    base: Path = tmp_path_factory.mktemp("cfg")
    backend = MemoryKeyring()
    manager = SettingsManager(base_dir=base, keyring_backend=backend)
    manager.set_api_key("openai_compat", secret)
    original.ai.api_key_ref = manager.api_key_ref("openai_compat")

    manager.save(original)
    text = manager.settings_path.read_text(encoding="utf-8")

    assert "keyring:DocSorter/openai_compat" in text
    assert secret not in text
    assert backend.store[("DocSorter", "openai_compat")] == secret


def test_yaml_cannot_round_trip_unicode_line_breaks(tmp_path_factory) -> None:
    """显式记录一条 YAML 的限制，而不是让它藏在生成器的约束里。

    U+0085(NEL)、U+2028(LS)、U+2029(PS) 在 YAML 规范里是换行字符，写入后读回会变成
    普通空格。这三个字符不会出现在真实的 Windows 路径里，因此对本项目无实际影响；
    但如果哪天配置里要存任意用户文本（比如自定义提示词），就必须改用 JSON 或做转义。
    """
    base: Path = tmp_path_factory.mktemp("cfg")
    manager = SettingsManager(base_dir=base, keyring_backend=MemoryKeyring())
    original = Settings()
    original.ui.recent_roots = ("\x85", "\u2028", "\u2029")

    manager.save(original)
    restored = manager.load()

    assert restored.ui.recent_roots != original.ui.recent_roots
    assert all(value.strip() == "" for value in restored.ui.recent_roots)


# ---------------------------------------------------------------------------
# 属性 42
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(gaps=st.lists(st.integers(min_value=0, max_value=900), min_size=1, max_size=300))
def test_streaming_notifications_respect_the_interval(gaps: list[int]) -> None:
    """相邻两次流式通知的间隔不小于 200ms。

    终止通知（final=True）不参与这条约束——见 progress.py 里对属性 42 的澄清：
    要保住终值就必须在最后无条件发一次。
    """
    clock = FakeClock()
    stamps: list[float] = []

    def sink(snapshot: ProgressSnapshot) -> None:
        if not snapshot.final:
            stamps.append(clock.now)

    throttle = ProgressThrottle(sink, clock=clock)
    for index, gap in enumerate(gaps, start=1):
        clock.advance_ms(gap)
        throttle.update(ProgressSnapshot("scan", index, len(gaps)))

    deltas = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    assert all(d >= DEFAULT_INTERVAL_MS / 1000.0 - 1e-9 for d in deltas)


@settings(max_examples=100, deadline=None)
@given(gaps=st.lists(st.integers(min_value=0, max_value=900), min_size=1, max_size=300))
def test_final_count_equals_event_total(gaps: list[int]) -> None:
    """节流只合并中间状态，不丢计数。"""
    clock = FakeClock()
    seen: list[ProgressSnapshot] = []
    throttle = ProgressThrottle(seen.append, clock=clock)

    total = len(gaps)
    for index, gap in enumerate(gaps, start=1):
        clock.advance_ms(gap)
        throttle.update(ProgressSnapshot("scan", index, total))
    throttle.finish()

    assert seen
    assert seen[-1].processed == total
    assert seen[-1].final is True


@settings(max_examples=100, deadline=None)
@given(gaps=st.lists(st.integers(min_value=0, max_value=900), min_size=1, max_size=200))
def test_reported_counts_are_monotonic(gaps: list[int]) -> None:
    clock = FakeClock()
    seen: list[ProgressSnapshot] = []
    throttle = ProgressThrottle(seen.append, clock=clock)

    for index, gap in enumerate(gaps, start=1):
        clock.advance_ms(gap)
        throttle.update(ProgressSnapshot("scan", index, len(gaps)))
    throttle.finish()

    counts = [s.processed for s in seen]
    assert counts == sorted(counts)
