"""RuleSerializer 与 RuleEngine 的测试。

属性 8（规则匹配与优先级）、9（规则序列化往返）、10（破坏容错）的 Hypothesis 版本
在任务 21。本文件覆盖具体例子与错误定位——需求 4.3 要求报出行号与字段名，那是
用户改坏自己规则文件时唯一有用的信息。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config.settings import packaged_rules_path
from app.core.rules import (
    PRIORITY_EXTENSION,
    PRIORITY_KEYWORD,
    Rule,
    RuleEngine,
    RuleSerializer,
    builtin_rules,
)

GOOD = """
version: 1
rules:
  - id: fin_invoice
    type: keyword
    priority: 200
    category: 财务/发票
    patterns: [发票, invoice]
  - id: doc_pdf
    type: extension
    priority: 100
    category: 文档/PDF
    patterns: ['.pdf']
"""


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def test_parses_valid_rules() -> None:
    rules, errors = RuleSerializer.load(GOOD)

    assert errors == []
    assert [r.id for r in rules] == ["fin_invoice", "doc_pdf"]
    assert rules[0].category == ("财务", "发票")
    assert rules[1].patterns == (".pdf",)


def test_category_accepts_list_form() -> None:
    rules, errors = RuleSerializer.load(
        "rules:\n  - {id: a, type: keyword, category: [财务, 发票], patterns: [x]}\n"
    )

    assert errors == []
    assert rules[0].category == ("财务", "发票")


def test_extension_patterns_are_normalised() -> None:
    """容忍用户写 pdf 或 .PDF。"""
    rules, errors = RuleSerializer.load(
        "rules:\n  - {id: a, type: extension, category: 文档, patterns: [PDF, '.DocX']}\n"
    )

    assert errors == []
    assert rules[0].patterns == (".pdf", ".docx")


def test_priority_defaults_by_type() -> None:
    """需求 3.9：关键词规则默认优先级高于扩展名规则。"""
    rules, _ = RuleSerializer.load(
        "rules:\n"
        "  - {id: k, type: keyword, category: A, patterns: [x]}\n"
        "  - {id: e, type: extension, category: B, patterns: ['.x']}\n"
    )
    by_id = {r.id: r for r in rules}

    assert by_id["k"].priority == PRIORITY_KEYWORD
    assert by_id["e"].priority == PRIORITY_EXTENSION
    assert by_id["k"].priority > by_id["e"].priority


# ---------------------------------------------------------------------------
# 错误定位（需求 4.3）
# ---------------------------------------------------------------------------


def test_syntax_error_reports_line() -> None:
    rules, errors = RuleSerializer.load("rules:\n  - [unclosed\n")

    assert rules == []
    assert errors
    assert errors[0].line is not None


def test_missing_field_reports_field_name() -> None:
    rules, errors = RuleSerializer.load(
        "rules:\n  - {id: a, type: keyword, category: A}\n"
    )

    assert rules == []
    assert any(e.field == "patterns" for e in errors)


def test_bad_type_reports_field_and_value() -> None:
    _, errors = RuleSerializer.load(
        "rules:\n  - {id: a, type: 乱来, category: A, patterns: [x]}\n"
    )

    assert any(e.field == "type" for e in errors)
    assert any("乱来" in e.message for e in errors)


def test_bad_regex_is_reported() -> None:
    _, errors = RuleSerializer.load(
        "rules:\n  - {id: a, type: regex, category: A, patterns: ['[unclosed']}\n"
    )

    assert any(e.field == "patterns" for e in errors)


def test_unknown_field_is_reported() -> None:
    _, errors = RuleSerializer.load(
        "rules:\n  - {id: a, type: keyword, category: A, patterns: [x], 多余: 1}\n"
    )

    assert any(e.field == "多余" for e in errors)


def test_duplicate_id_is_reported() -> None:
    _, errors = RuleSerializer.load(
        "rules:\n"
        "  - {id: same, type: keyword, category: A, patterns: [x]}\n"
        "  - {id: same, type: keyword, category: B, patterns: [y]}\n"
    )

    assert any("重复" in e.message for e in errors)


def test_missing_rules_key_is_reported() -> None:
    _, errors = RuleSerializer.load("version: 1\n")

    assert any(e.field == "rules" for e in errors)


def test_empty_file_is_reported() -> None:
    rules, errors = RuleSerializer.load("")

    assert rules == []
    assert errors


def test_non_mapping_root_is_reported() -> None:
    _, errors = RuleSerializer.load("- a\n- b\n")

    assert errors


def test_error_describe_is_human_readable() -> None:
    _, errors = RuleSerializer.load(
        "rules:\n  - {id: a, type: keyword, category: A}\n"
    )

    text = errors[0].describe()
    assert "行" in text
    assert "patterns" in text


def test_partial_validity_is_rejected_wholesale() -> None:
    """需求 4.3：校验失败整体回落，不做部分采纳。

    半套规则会产出用户看不懂的分类结果——一半文件按新规则走、一半按旧规则走。
    """
    rules, errors = RuleSerializer.load(
        "rules:\n"
        "  - {id: ok, type: keyword, category: A, patterns: [x]}\n"
        "  - {id: bad, type: 乱来, category: B, patterns: [y]}\n"
    )

    assert rules == []
    assert errors


def test_load_never_raises_on_junk() -> None:
    for payload in ("", "\t\t", "rules: 1", "rules:\n  - 5", "?: :", "{{{"):
        rules, errors = RuleSerializer.load(payload)
        assert isinstance(rules, list)
        assert isinstance(errors, list)


# ---------------------------------------------------------------------------
# 往返（需求 4.5）
# ---------------------------------------------------------------------------


def test_round_trip_preserves_rule_set() -> None:
    original = builtin_rules()

    text = RuleSerializer.dump(original)
    restored, errors = RuleSerializer.load(text)

    assert errors == []
    assert set(restored) == set(original)


def test_round_trip_preserves_all_six_fields() -> None:
    rule = Rule(
        id="custom",
        type="regex",
        priority=175,
        patterns=(r"^\d{4}-发票",),
        category=("财务", "增值税发票"),
        enabled=False,
    )

    restored, errors = RuleSerializer.load(RuleSerializer.dump([rule]))

    assert errors == []
    assert restored == [rule]


def test_dump_is_deterministic() -> None:
    rules = builtin_rules()

    assert RuleSerializer.dump(rules) == RuleSerializer.dump(list(reversed(rules)))


def test_dump_allows_unicode() -> None:
    text = RuleSerializer.dump(builtin_rules())

    assert "财务/发票" in text
    assert "!!python" not in text


# ---------------------------------------------------------------------------
# 内置规则（需求 4.6、4.7）
# ---------------------------------------------------------------------------


def test_builtin_covers_six_keyword_categories() -> None:
    categories = {"/".join(r.category) for r in builtin_rules() if r.type != "extension"}

    assert {
        "财务/发票",
        "财务/报销",
        "财务/对账单",
        "合同协议",
        "法律文书",
        "简历",
        "证件资料",
        "邮件",
        "会议记录",
        "笔记",
        "截图",
    }.issubset(categories)


def test_builtin_covers_eleven_extension_categories() -> None:
    categories = {
        "/".join(r.category) for r in builtin_rules() if r.type == "extension"
    }

    assert {
        "文档/PDF",
        "文档/Word",
        "文档/表格",
        "文档/演示",
        "文档/文本",
        "电子书",
        "图片",
        "音视频",
        "压缩包",
        "安装程序",
        "代码",
        "设计",
        "字体",
        "镜像",
    }.issubset(categories)


def test_packaged_rules_file_exists_and_parses() -> None:
    """rules_default.yaml 必须存在且合法——它是规则文件缺失时的兜底来源。"""
    path = packaged_rules_path()

    assert path.exists(), f"缺少内置规则文件: {path}"
    rules, errors = RuleSerializer.load(path.read_text(encoding="utf-8"))

    assert errors == [], [e.describe() for e in errors]
    assert set(rules) == set(builtin_rules()), "内置规则文件与 builtin_rules() 不一致"


def test_default_preset_equals_builtin_rules() -> None:
    """通用办公方案即内置默认规则。"""
    from app.core.rules import DEFAULT_PRESET_ID, RULE_PRESETS, preset_by_id

    assert DEFAULT_PRESET_ID == RULE_PRESETS[0].id
    assert list(preset_by_id(DEFAULT_PRESET_ID).rules) == builtin_rules()


def test_presets_have_unique_ids_and_valid_rules() -> None:
    """每套方案的规则都必须合法可序列化，且 id 唯一。"""
    from app.core.rules import RULE_PRESETS

    ids = [p.id for p in RULE_PRESETS]
    assert len(ids) == len(set(ids))

    for preset in RULE_PRESETS:
        assert preset.name
        assert preset.description
        text = RuleSerializer.dump(preset.rules)
        restored, errors = RuleSerializer.load(text)
        assert errors == [], [e.describe() for e in errors]
        assert set(restored) == set(preset.rules)
        # 每套方案至少有一条规则，否则这套方案没有意义
        assert len(preset.rules) > 0


def test_presets_differ_in_character() -> None:
    """不同方案应具有不同特点（规则集不应完全相同）。"""
    from app.core.rules import RULE_PRESETS

    signature = {
        p.id: {(r.id, r.type, r.category) for r in p.rules}
        for p in RULE_PRESETS
    }
    values = list(signature.values())
    for i in range(len(values)):
        for j in range(i + 1, len(values)):
            assert values[i] != values[j], "存在完全相同的两套方案"


def test_preset_by_id_unknown_returns_none() -> None:
    from app.core.rules import preset_by_id

    assert preset_by_id("does_not_exist") is None


def test_developer_preset_has_broad_code_rules() -> None:
    """开发者方案应覆盖大量代码扩展名。"""
    from app.core.rules import preset_by_id

    preset = preset_by_id("developer")
    code_rules = [r for r in preset.rules if r.id == "code"]
    assert code_rules and len(code_rules[0].patterns) >= 30


# ---------------------------------------------------------------------------
# RuleEngine
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> RuleEngine:
    return RuleEngine()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("2024年3月发票.pdf", ("财务", "发票")),
        ("INVOICE_001.pdf", ("财务", "发票")),
        ("差旅报销单.xlsx", ("财务", "报销")),
        ("采购合同_final.docx", ("合同协议",)),
        ("张三的简历.pdf", ("简历",)),
        ("Resume_2024.docx", ("简历",)),
        ("身份证正面.jpg", ("证件资料",)),
    ],
)
def test_keyword_matching(engine: RuleEngine, name: str, expected: tuple[str, ...]) -> None:
    hit = engine.match_name(name)

    assert hit is not None
    assert hit.rule.category == expected


@pytest.mark.parametrize(
    "name",
    ["Screenshot_20240301.png", "屏幕截图 2024-03-01.png", "image_12.png", "截图1.png"],
)
def test_screenshot_regex_matching(engine: RuleEngine, name: str) -> None:
    hit = engine.match_name(name)

    assert hit is not None
    assert hit.rule.category == ("截图",)


def test_matching_ignores_case(engine: RuleEngine) -> None:
    """需求 4.9。"""
    assert engine.match_name("CONTRACT.pdf") is not None
    assert engine.match_name("contract.pdf") is not None
    assert engine.match_name("Contract.pdf") is not None


def test_unmatched_name_returns_none(engine: RuleEngine) -> None:
    assert engine.match_name("aaa.bbb") is None


@pytest.mark.parametrize(
    ("ext", "expected"),
    [
        (".pdf", ("文档", "PDF")),
        (".PDF", ("文档", "PDF")),
        ("pdf", ("文档", "PDF")),
        (".docx", ("文档", "Word")),
        (".xlsx", ("文档", "表格")),
        (".png", ("图片",)),
        (".mp4", ("音视频",)),
        (".zip", ("压缩包",)),
        (".py", ("代码",)),
        (".epub", ("电子书",)),
    ],
)
def test_extension_matching(engine: RuleEngine, ext: str, expected: tuple[str, ...]) -> None:
    hit = engine.match_ext(ext)

    assert hit is not None
    assert hit.rule.category == expected


def test_unknown_extension_returns_none(engine: RuleEngine) -> None:
    assert engine.match_ext(".zzz") is None


def test_match_text_uses_keywords_only(engine: RuleEngine) -> None:
    """正文匹配刻意不用正则：截图那条 `^screenshot` 套到正文上没有意义。"""
    assert engine.match_text("本次采购合同约定如下") is not None
    assert engine.match_text("screenshot 出现在正文中间") is None


def test_disabled_rules_are_ignored() -> None:
    engine = RuleEngine(
        [
            Rule("off", "keyword", 200, ("发票",), ("财务", "发票"), enabled=False),
            Rule("on", "extension", 100, (".pdf",), ("文档", "PDF")),
        ]
    )

    assert engine.match_name("发票.pdf") is None
    assert engine.match_ext(".pdf") is not None


def test_higher_priority_wins() -> None:
    engine = RuleEngine(
        [
            Rule("low", "keyword", 50, ("报告",), ("低",)),
            Rule("high", "keyword", 300, ("报告",), ("高",)),
        ]
    )

    hit = engine.match_name("季度报告.docx")

    assert hit is not None
    assert hit.rule.category == ("高",)


def test_same_priority_keeps_declaration_order() -> None:
    engine = RuleEngine(
        [
            Rule("first", "keyword", 200, ("报告",), ("先",)),
            Rule("second", "keyword", 200, ("报告",), ("后",)),
        ]
    )

    hit = engine.match_name("报告.docx")

    assert hit is not None
    assert hit.rule.category == ("先",)


def test_engine_from_text_falls_back_on_error() -> None:
    """需求 4.3：解析失败仍要能完成分类。"""
    engine, errors = RuleEngine.from_text("rules:\n  - {id: a, type: 乱来}\n")

    assert errors
    assert engine.match_ext(".pdf") is not None  # 内置规则仍然可用


def test_engine_from_text_uses_custom_rules_when_valid() -> None:
    engine, errors = RuleEngine.from_text(
        "rules:\n  - {id: only, type: extension, category: 我的, patterns: ['.pdf']}\n"
    )

    assert errors == []
    hit = engine.match_ext(".pdf")
    assert hit is not None
    assert hit.rule.category == ("我的",)
    assert engine.match_ext(".png") is None


def test_categories_order_is_stable(engine: RuleEngine) -> None:
    assert engine.categories() == engine.categories()
