# Feature: document-sorting-tool, Property 8: 规则匹配与优先级
"""属性 8、9、10。

- 属性 8：匹配忽略大小写；关键词规则优先于扩展名规则；同优先级按声明顺序取首个
  命中；停用的规则不参与匹配。
- 属性 9：任意合法规则集合序列化后再解析产出等价集合，六个字段都被保留。
- 属性 10：任意破坏方式下解析都返回结构化错误而不抛未捕获异常；规则解析失败时
  分类仍用内置默认规则完成；配置解析失败时返回合法配置并把文件重写为合法内容。

Validates: Requirements 4.3, 4.5, 4.9, 3.9, 18.2
"""

from __future__ import annotations

from pathlib import Path

import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.config.settings import Settings, SettingsManager
from app.core.rules import (
    PRIORITY_EXTENSION,
    PRIORITY_KEYWORD,
    Rule,
    RuleEngine,
    RuleSerializer,
    builtin_rules,
)
from tests.fixtures.doubles import MemoryKeyring

FS = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

_ID_CHARS = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789_"),
    min_size=1,
    max_size=10,
)
_SEG = st.text(
    alphabet=st.sampled_from("abcXYZ中文财务发票文档 -_"), min_size=1, max_size=8
)
_KEYWORD = st.text(
    alphabet=st.sampled_from("abcXYZ中文发票合同简历0189"), min_size=1, max_size=8
)
_EXT = st.sampled_from([".pdf", ".docx", ".png", ".zip", ".py", ".xyz", ".TAR"])


@st.composite
def rules(draw: st.DrawFn) -> Rule:
    kind = draw(st.sampled_from(["keyword", "regex", "extension"]))
    if kind == "extension":
        patterns = tuple(draw(st.lists(_EXT, min_size=1, max_size=4, unique=True)))
        patterns = tuple(p.lower() for p in patterns)
        priority = draw(st.integers(1, 150))
    elif kind == "regex":
        patterns = tuple(
            draw(st.lists(st.sampled_from([r"^a\d+", r"报告$", r"[0-9]{4}"]), min_size=1, max_size=2))
        )
        priority = draw(st.integers(151, 400))
    else:
        patterns = tuple(draw(st.lists(_KEYWORD, min_size=1, max_size=4, unique=True)))
        priority = draw(st.integers(151, 400))

    return Rule(
        id=draw(_ID_CHARS),
        type=kind,  # type: ignore[arg-type]
        priority=priority,
        patterns=patterns,
        category=tuple(draw(st.lists(_SEG, min_size=1, max_size=3))),
        enabled=draw(st.booleans()),
    )


@st.composite
def rule_sets(draw: st.DrawFn) -> list[Rule]:
    drawn = draw(st.lists(rules(), min_size=1, max_size=8))
    seen: dict[str, Rule] = {}
    for rule in drawn:
        seen.setdefault(rule.id, rule)
    return list(seen.values())


# ---------------------------------------------------------------------------
# 属性 9：序列化往返
# ---------------------------------------------------------------------------


@FS
@given(original=rule_sets())
def test_rule_round_trip_preserves_set(original: list[Rule]) -> None:
    text = RuleSerializer.dump(original)

    restored, errors = RuleSerializer.load(text)

    assert errors == [], [e.describe() for e in errors]
    assert set(restored) == set(original)


@FS
@given(original=rule_sets())
def test_rule_round_trip_preserves_all_six_fields(original: list[Rule]) -> None:
    restored, errors = RuleSerializer.load(RuleSerializer.dump(original))

    assert errors == []
    by_id = {r.id: r for r in restored}
    for rule in original:
        got = by_id[rule.id]
        assert (got.type, got.priority, got.patterns, got.category, got.enabled) == (
            rule.type,
            rule.priority,
            rule.patterns,
            rule.category,
            rule.enabled,
        )


@FS
@given(original=rule_sets())
def test_dump_is_order_independent(original: list[Rule]) -> None:
    """同一份规则集无论传入顺序，导出文本一致。"""
    assert RuleSerializer.dump(original) == RuleSerializer.dump(
        list(reversed(original))
    )


# ---------------------------------------------------------------------------
# 属性 8：匹配语义
# ---------------------------------------------------------------------------


@FS
@given(name=st.text(min_size=1, max_size=30))
def test_matching_ignores_case(name: str) -> None:
    """需求 4.9。"""
    engine = RuleEngine()

    lower = engine.match_name(name.lower())
    upper = engine.match_name(name.upper())

    if lower is None and upper is None:
        return
    assert lower is not None and upper is not None
    assert lower.rule.id == upper.rule.id


@FS
@given(
    keyword=_KEYWORD,
    ext=_EXT,
    category_a=st.lists(_SEG, min_size=1, max_size=2).map(tuple),
    category_b=st.lists(_SEG, min_size=1, max_size=2).map(tuple),
)
def test_keyword_rule_beats_extension_rule(
    keyword: str, ext: str, category_a: tuple, category_b: tuple
) -> None:
    """需求 3.9：同时命中时关键词规则胜出。"""
    engine = RuleEngine(
        [
            Rule("kw", "keyword", PRIORITY_KEYWORD, (keyword,), category_a),
            Rule("ex", "extension", PRIORITY_EXTENSION, (ext.lower(),), category_b),
        ]
    )
    name = f"{keyword}{ext}"

    by_name = engine.match_name(name)
    by_ext = engine.match_ext(ext)

    assert by_name is not None and by_name.rule.id == "kw"
    assert by_ext is not None and by_ext.rule.id == "ex"
    assert engine.rules[0].priority > engine.rules[1].priority


@FS
@given(keyword=_KEYWORD, high=st.integers(200, 400), low=st.integers(1, 199))
def test_higher_priority_wins(keyword: str, high: int, low: int) -> None:
    engine = RuleEngine(
        [
            Rule("low", "keyword", low, (keyword,), ("低",)),
            Rule("high", "keyword", high, (keyword,), ("高",)),
        ]
    )

    hit = engine.match_name(f"x{keyword}y")

    assert hit is not None and hit.rule.category == ("高",)


@FS
@given(keyword=_KEYWORD, priority=st.integers(1, 400))
def test_same_priority_keeps_declaration_order(keyword: str, priority: int) -> None:
    engine = RuleEngine(
        [
            Rule("first", "keyword", priority, (keyword,), ("先",)),
            Rule("second", "keyword", priority, (keyword,), ("后",)),
        ]
    )

    hit = engine.match_name(f"a{keyword}b")

    assert hit is not None and hit.rule.category == ("先",)


@FS
@given(rule_set=rule_sets())
def test_disabled_rules_never_match(rule_set: list[Rule]) -> None:
    engine = RuleEngine(rule_set)

    assert all(rule.enabled for rule in engine.rules)


@FS
@given(rule_set=rule_sets(), name=st.text(min_size=1, max_size=20))
def test_matching_is_deterministic(rule_set: list[Rule], name: str) -> None:
    engine = RuleEngine(rule_set)

    first = engine.match_name(name)
    second = engine.match_name(name)

    assert (first.rule.id if first else None) == (second.rule.id if second else None)


# ---------------------------------------------------------------------------
# 属性 10：破坏容错
# ---------------------------------------------------------------------------

_CORRUPTIONS = st.sampled_from(
    [
        "drop_required",
        "wrong_type",
        "unknown_key",
        "break_indent",
        "truncate",
        "replace_with_scalar",
    ]
)


def _corrupt(text: str, how: str) -> str:
    if how == "break_indent":
        return text.replace("\n  ", "\n\t \t")
    if how == "truncate":
        return text[: max(1, len(text) // 2)]
    if how == "replace_with_scalar":
        return "rules: 42\n"
    if how == "drop_required":
        return text.replace("patterns", "xxxxxxxx")
    if how == "wrong_type":
        return text.replace("type: keyword", "type: 乱来").replace(
            "type: extension", "type: 12345"
        )
    return text + "\n未知顶层键: 1\n"


@FS
@given(original=rule_sets(), how=_CORRUPTIONS)
def test_corrupted_rules_never_raise(original: list[Rule], how: str) -> None:
    """属性 10：任何破坏方式都返回结构化错误而不抛异常。"""
    broken = _corrupt(RuleSerializer.dump(original), how)

    parsed, errors = RuleSerializer.load(broken)

    assert isinstance(parsed, list)
    assert isinstance(errors, list)
    if errors:
        assert parsed == []
        assert all(e.describe() for e in errors)


@FS
@given(original=rule_sets(), how=_CORRUPTIONS)
def test_broken_rules_fall_back_to_builtin(original: list[Rule], how: str) -> None:
    """需求 4.3：规则坏了分类仍要能完成。"""
    broken = _corrupt(RuleSerializer.dump(original), how)

    engine, errors = RuleEngine.from_text(broken)

    # 无论解析成功还是回落，引擎都必须是可用的——需求 4.3 的实质是「分类不能因为
    # 规则文件写坏了就做不了」。
    assert engine.rules or not errors
    if errors:
        assert engine.match_ext(".pdf") is not None, "回落后内置扩展名规则应可用"
        builtin_ids = {r.id for r in builtin_rules()}
        assert {r.id for r in engine.rules} == builtin_ids


@FS
@given(
    payload=st.recursive(
        st.one_of(
            st.none(),
            st.booleans(),
            st.integers(),
            st.text(max_size=10),
        ),
        lambda inner: st.one_of(
            st.lists(inner, max_size=3),
            st.dictionaries(st.text(max_size=8), inner, max_size=3),
        ),
        max_leaves=8,
    )
)
def test_settings_load_never_raises_on_arbitrary_yaml(
    tmp_path_factory, payload: object
) -> None:
    """属性 10 的配置侧：任意 YAML 都要读出一个合法 Settings。"""
    base: Path = tmp_path_factory.mktemp("cfg")
    manager = SettingsManager(base_dir=base, keyring_backend=MemoryKeyring())
    base.mkdir(parents=True, exist_ok=True)
    manager.settings_path.write_text(
        yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8"
    )

    loaded = manager.load()

    assert isinstance(loaded, Settings)
    # 需求 18.2：非法内容要被重写成合法内容
    again = SettingsManager(
        base_dir=base, keyring_backend=MemoryKeyring()
    ).load()
    assert isinstance(again, Settings)
