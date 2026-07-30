"""Rule_Engine 与 Rule_Serializer。

## 为什么解析要带行号

需求 4.3 要求规则文件出错时给出**行号与字段名**。用 ``yaml.safe_load`` 只能拿到
一个嵌套 dict，行号信息在解析时就丢了；所以这里用 ``yaml.compose()`` 拿到保留了
``start_mark`` 的节点树，再逐字段校验。用户改坏了自己的规则文件时，「第 17 行
patterns 字段不是列表」比「配置无效」有用得多。

## 解析失败不阻断分类

校验失败返回空规则集 + 错误列表，调用方改用内置默认规则完成本次分类
（需求 4.3、属性 10）。规则文件写坏了不该让工具不能用。

## 往返的等价定义

需求 4.5 的往返一致性比较的是**规则对象集合相等**，不是 YAML 文本逐字节相等：
注释与键序不参与比较。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple

import yaml

RULES_VERSION = 1

RuleType = Literal["keyword", "regex", "extension"]
_VALID_TYPES: frozenset[str] = frozenset({"keyword", "regex", "extension"})

#: 需求 3.9：关键词规则优先级高于扩展名规则
PRIORITY_KEYWORD = 200
PRIORITY_EXTENSION = 100

#: 类目路径在 YAML 里写成 "财务/发票"。这是**文件格式**的表示，读进来立刻拆成
#: 元组；内存里一律用元组，避免分隔符歧义（见 models.Category 的说明）。
CATEGORY_SEPARATOR = "/"


@dataclass(frozen=True)
class Rule:
    id: str
    type: RuleType
    priority: int
    patterns: tuple[str, ...]
    category: tuple[str, ...]
    enabled: bool = True


@dataclass(frozen=True)
class RuleError:
    line: int | None
    field: str | None
    message: str

    def describe(self) -> str:
        where = f"第 {self.line} 行" if self.line is not None else "文件"
        what = f" 的 {self.field} 字段" if self.field else ""
        return f"{where}{what}：{self.message}"


class RuleHit(NamedTuple):
    rule: Rule
    matched: str  # 命中的具体 pattern，用于组装 reason


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------


def _line_of(node: Any) -> int | None:
    mark = getattr(node, "start_mark", None)
    return None if mark is None else mark.line + 1


def _scalar(node: Any) -> Any:
    """把 compose 出来的标量节点转成 Python 值。"""
    return yaml.safe_load(yaml.serialize(node)) if node is not None else None


class RuleSerializer:
    """规则文件的解析与序列化。"""

    @staticmethod
    def load(text: str) -> tuple[list[Rule], list[RuleError]]:
        """解析规则文本。需求 4.2、4.3。

        任何形式的损坏都返回 ``([], errors)`` 而不抛异常。
        """
        try:
            root = yaml.compose(text)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            line = None if mark is None else mark.line + 1
            return [], [RuleError(line, None, f"YAML 语法错误：{exc}")]

        if root is None:
            return [], [RuleError(None, None, "规则文件为空")]
        if not isinstance(root, yaml.MappingNode):
            return [], [RuleError(_line_of(root), None, "顶层结构必须是映射")]

        fields = {
            _scalar(key): (value, _line_of(key))
            for key, value in root.value
        }

        if "rules" not in fields:
            return [], [RuleError(_line_of(root), "rules", "缺少 rules 字段")]

        rules_node, rules_line = fields["rules"]
        if not isinstance(rules_node, yaml.SequenceNode):
            return [], [RuleError(rules_line, "rules", "rules 必须是列表")]

        rules: list[Rule] = []
        errors: list[RuleError] = []
        seen_ids: set[str] = set()

        for item in rules_node.value:
            rule, item_errors = RuleSerializer._parse_rule(item)
            errors.extend(item_errors)
            if rule is None:
                continue
            if rule.id in seen_ids:
                errors.append(
                    RuleError(_line_of(item), "id", f"规则 id 重复：{rule.id}")
                )
                continue
            seen_ids.add(rule.id)
            rules.append(rule)

        if errors:
            # 需求 4.3：校验失败整体回落到内置默认规则，不做部分采纳——
            # 半套规则会产出用户看不懂的分类结果。
            return [], errors
        return rules, []

    @staticmethod
    def _parse_rule(node: Any) -> tuple[Rule | None, list[RuleError]]:
        line = _line_of(node)
        if not isinstance(node, yaml.MappingNode):
            return None, [RuleError(line, None, "规则条目必须是映射")]

        raw: dict[str, tuple[Any, int | None]] = {}
        for key, value in node.value:
            raw[str(_scalar(key))] = (value, _line_of(key))

        errors: list[RuleError] = []

        def require(name: str) -> tuple[Any, int | None] | None:
            if name not in raw:
                errors.append(RuleError(line, name, f"缺少 {name} 字段"))
                return None
            return raw[name]

        rule_id_entry = require("id")
        type_entry = require("type")
        category_entry = require("category")
        patterns_entry = require("patterns")
        if errors:
            return None, errors

        assert rule_id_entry and type_entry and category_entry and patterns_entry

        rule_id = _scalar(rule_id_entry[0])
        if not isinstance(rule_id, str) or not rule_id.strip():
            errors.append(RuleError(rule_id_entry[1], "id", "id 必须是非空字符串"))

        rule_type = _scalar(type_entry[0])
        if rule_type not in _VALID_TYPES:
            errors.append(
                RuleError(
                    type_entry[1],
                    "type",
                    f"type 必须是 {sorted(_VALID_TYPES)} 之一，实际是 {rule_type!r}",
                )
            )

        category_raw = _scalar(category_entry[0])
        category: tuple[str, ...] = ()
        if isinstance(category_raw, str):
            category = tuple(
                seg for seg in category_raw.split(CATEGORY_SEPARATOR) if seg
            )
        elif isinstance(category_raw, list) and all(
            isinstance(s, str) for s in category_raw
        ):
            category = tuple(s for s in category_raw if s)
        else:
            errors.append(
                RuleError(category_entry[1], "category", "category 必须是字符串或字符串列表")
            )
        if not category and not errors:
            errors.append(RuleError(category_entry[1], "category", "category 不能为空"))

        patterns_raw = _scalar(patterns_entry[0])
        patterns: tuple[str, ...] = ()
        if isinstance(patterns_raw, list) and all(
            isinstance(p, str) for p in patterns_raw
        ):
            patterns = tuple(p for p in patterns_raw if p)
        else:
            errors.append(
                RuleError(patterns_entry[1], "patterns", "patterns 必须是字符串列表")
            )
        if not patterns and not errors:
            errors.append(RuleError(patterns_entry[1], "patterns", "patterns 不能为空"))

        if rule_type == "regex":
            for pattern in patterns:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    errors.append(
                        RuleError(
                            patterns_entry[1],
                            "patterns",
                            f"正则 {pattern!r} 无法编译：{exc}",
                        )
                    )

        default_priority = (
            PRIORITY_EXTENSION if rule_type == "extension" else PRIORITY_KEYWORD
        )
        priority = default_priority
        if "priority" in raw:
            value = _scalar(raw["priority"][0])
            if isinstance(value, bool) or not isinstance(value, int):
                errors.append(RuleError(raw["priority"][1], "priority", "priority 必须是整数"))
            else:
                priority = value

        enabled = True
        if "enabled" in raw:
            value = _scalar(raw["enabled"][0])
            if not isinstance(value, bool):
                errors.append(RuleError(raw["enabled"][1], "enabled", "enabled 必须是布尔值"))
            else:
                enabled = value

        for key in raw:
            if key not in {"id", "type", "priority", "patterns", "category", "enabled"}:
                errors.append(RuleError(raw[key][1], key, f"未知字段 {key}"))

        if errors:
            return None, errors

        normalized = (
            tuple(_normalize_ext(p) for p in patterns)
            if rule_type == "extension"
            else patterns
        )
        return (
            Rule(
                id=str(rule_id),
                type=rule_type,  # type: ignore[arg-type]
                priority=priority,
                patterns=normalized,
                category=category,
                enabled=enabled,
            ),
            [],
        )

    @staticmethod
    def dump(rules: Sequence[Rule]) -> str:
        """序列化为 YAML。需求 4.4。

        按 ``(priority 降序, id)`` 排序输出，使同一份规则集每次导出的文本一致。
        """
        ordered = sorted(rules, key=lambda r: (-r.priority, r.id))
        payload = {
            "version": RULES_VERSION,
            "rules": [
                {
                    "id": rule.id,
                    "type": rule.type,
                    "priority": rule.priority,
                    "category": CATEGORY_SEPARATOR.join(rule.category),
                    "patterns": list(rule.patterns),
                    "enabled": rule.enabled,
                }
                for rule in ordered
            ],
        }
        return yaml.safe_dump(
            payload, sort_keys=False, allow_unicode=True, default_flow_style=False
        )


def _normalize_ext(ext: str) -> str:
    """扩展名统一成小写含点的形式，容忍用户写 ``pdf`` 或 ``.PDF``。"""
    value = ext.strip().lower()
    if not value:
        return value
    return value if value.startswith(".") else f".{value}"


# ---------------------------------------------------------------------------
# 内置默认规则
# ---------------------------------------------------------------------------

#: 需求 4.6 的 6 组关键词类目
_KEYWORD_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("fin_invoice", ("财务", "发票"), ("发票", "invoice", "税票")),
    ("fin_expense", ("财务", "报销"), ("报销", "费用", "expense")),
    ("legal_contract", ("合同协议",), ("合同", "协议", "contract", "agreement", "nda")),
    ("hr_resume", ("简历",), ("简历", "resume", "cv")),
    ("id_docs", ("证件资料",), ("身份证", "护照", "户口", "营业执照")),
)

#: 需求 4.6 的截图规则（正则）
_REGEX_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "shot_screenshot",
        ("截图",),
        (r"^(screenshot|屏幕截图|截图|image_?\d+)",),
    ),
)

#: 需求 4.7 的 11 组扩展名类目
_EXTENSION_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("doc_pdf", ("文档", "PDF"), (".pdf",)),
    ("doc_word", ("文档", "Word"), (".doc", ".docx", ".rtf", ".odt")),
    ("doc_sheet", ("文档", "表格"), (".xls", ".xlsx", ".csv", ".ods")),
    ("doc_slide", ("文档", "演示"), (".ppt", ".pptx", ".odp", ".key")),
    ("doc_text", ("文档", "文本"), (".txt", ".md", ".log")),
    ("ebook", ("电子书",), (".epub", ".mobi", ".azw3", ".djvu")),
    (
        "image",
        ("图片",),
        (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".svg", ".psd"),
    ),
    (
        "media",
        ("音视频",),
        (".mp3", ".wav", ".flac", ".mp4", ".mkv", ".avi", ".mov"),
    ),
    ("archive", ("压缩包",), (".zip", ".rar", ".7z", ".tar", ".gz")),
    ("installer", ("安装程序",), (".exe", ".msi", ".apk")),
    (
        "code",
        ("代码",),
        (
            ".py", ".js", ".ts", ".java", ".c", ".cpp", ".go", ".rs",
            ".html", ".css", ".json", ".xml", ".yaml", ".sql",
        ),
    ),
)


def builtin_rules() -> list[Rule]:
    """内置默认规则。需求 4.6、4.7。

    规则文件缺失或解析失败时用它兜底（需求 4.3），因此它必须是可用的完整规则集，
    而不是空集。
    """
    rules: list[Rule] = []
    for rule_id, category, patterns in _KEYWORD_RULES:
        rules.append(
            Rule(rule_id, "keyword", PRIORITY_KEYWORD, patterns, category)
        )
    for rule_id, category, patterns in _REGEX_RULES:
        rules.append(Rule(rule_id, "regex", PRIORITY_KEYWORD, patterns, category))
    for rule_id, category, patterns in _EXTENSION_RULES:
        rules.append(
            Rule(rule_id, "extension", PRIORITY_EXTENSION, patterns, category)
        )
    return rules


# ---------------------------------------------------------------------------
# RuleEngine
# ---------------------------------------------------------------------------


class RuleEngine:
    """规则匹配。

    匹配一律忽略大小写（需求 4.9）。同优先级内按规则在列表中的顺序取第一个命中，
    使结果确定（需求 3.13）——不依赖字典或集合的迭代顺序。
    """

    def __init__(self, rules: Sequence[Rule] | None = None) -> None:
        source = list(builtin_rules() if rules is None else rules)
        self._rules: tuple[Rule, ...] = tuple(r for r in source if r.enabled)

        # 稳定排序：priority 降序，同 priority 保持原始顺序
        ordered = sorted(
            enumerate(self._rules), key=lambda pair: (-pair[1].priority, pair[0])
        )
        self._ordered: tuple[Rule, ...] = tuple(rule for _, rule in ordered)

        self._keyword: tuple[tuple[Rule, tuple[str, ...]], ...] = tuple(
            (rule, tuple(p.lower() for p in rule.patterns))
            for rule in self._ordered
            if rule.type == "keyword"
        )
        self._regex: tuple[tuple[Rule, tuple[tuple[str, re.Pattern[str]], ...]], ...] = tuple(
            (
                rule,
                tuple(
                    (p, compiled)
                    for p in rule.patterns
                    if (compiled := _try_compile(p)) is not None
                ),
            )
            for rule in self._ordered
            if rule.type == "regex"
        )
        self._extension: dict[str, RuleHit] = {}
        for rule in self._ordered:
            if rule.type != "extension":
                continue
            for pattern in rule.patterns:
                # 先注册者优先，与 _ordered 的顺序一致
                self._extension.setdefault(pattern.lower(), RuleHit(rule, pattern))

    @classmethod
    def from_text(cls, text: str) -> tuple[RuleEngine, list[RuleError]]:
        """从规则文本构造。解析失败时回落到内置默认规则。需求 4.3。"""
        rules, errors = RuleSerializer.load(text)
        if errors:
            return cls(None), errors
        return cls(rules), []

    @property
    def rules(self) -> tuple[Rule, ...]:
        return self._rules

    def match_name(self, name: str) -> RuleHit | None:
        """按文件名匹配关键词与正则规则。需求 4.9。"""
        lowered = name.lower()
        for rule, patterns in self._keyword:
            for pattern in patterns:
                if pattern in lowered:
                    return RuleHit(rule, pattern)
        for rule, compiled in self._regex:
            for raw, expression in compiled:
                if expression.search(name) or expression.search(lowered):
                    return RuleHit(rule, raw)
        return None

    def match_text(self, text: str) -> RuleHit | None:
        """按正文匹配关键词规则。需求 5.5。

        刻意只用关键词而不用正则：正文里的正则规则（例如截图文件名的
        ``^screenshot``）是为文件名设计的，套到正文上会得到莫名其妙的命中。
        """
        lowered = text.lower()
        for rule, patterns in self._keyword:
            for pattern in patterns:
                if pattern in lowered:
                    return RuleHit(rule, pattern)
        return None

    def match_ext(self, ext: str) -> RuleHit | None:
        return self._extension.get(_normalize_ext(ext))

    def categories(self) -> tuple[tuple[str, ...], ...]:
        """全部规则涉及的类目，顺序稳定。"""
        seen: list[tuple[str, ...]] = []
        for rule in self._ordered:
            if rule.category not in seen:
                seen.append(rule.category)
        return tuple(seen)


def _try_compile(pattern: str) -> re.Pattern[str] | None:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None


def render_rules_yaml(rules: Iterable[Rule] | None = None) -> str:
    """生成内置规则文件的内容。供构建 ``rules_default.yaml`` 使用。"""
    return RuleSerializer.dump(list(rules if rules is not None else builtin_rules()))
