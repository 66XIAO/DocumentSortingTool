"""两阶段 taxonomy 调用与收敛。

需求 7 的 LLM 分类分两步走：
    第一阶段：抽样不超过 300 个文件摘要，请求模型产出 taxonomy（类目表）。
    第二阶段：全部文件摘要按固定 taxonomy 分批发送，要求模型只能从表中选。

收敛（需求 7.6、7.10、7.11）：
    类目数不超过 12，层级深度不超过 2。
    用编辑距离与同义词表把相近类目名归并；归并后仍超 12 则按文件数从少到多
    合并进 ``其他`` 直至不超过 12。
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from app.core.llm.provider import (
    ChatMessage,
    ChatRequest,
    FailureKind,
    Provider,
    ProviderError,
)
from app.core.models import FileEntry, PrivacyLevel

MAX_CATEGORIES: Final[int] = 12
MAX_DEPTH: Final[int] = 2
MAX_SAMPLE_SIZE: Final[int] = 300
MIN_BATCH_SIZE: Final[int] = 80
MAX_BATCH_SIZE: Final[int] = 120

UNKNOWN: Final[str] = "unknown"
OTHER_CATEGORY: Final[tuple[str, ...]] = ("其他",)


@dataclass(frozen=True)
class FileSummary:
    """发送给 LLM 的文件摘要。需求 8.1。"""

    name: str
    ext: str
    size: int
    mtime: str
    rel_path: str
    text_head: str | None = None


@dataclass
class Taxonomy:
    """LLM 产出的类目表。需求 7.5、7.6。

    categories 是类目路径元组的列表，每个路径不超过 2 级。
    """

    categories: list[tuple[str, ...]] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.categories)

    def has_category(self, path: tuple[str, ...]) -> bool:
        return path in self.categories

    def add_category(self, path: tuple[str, ...]) -> None:
        if path not in self.categories:
            self.categories.append(path)


@dataclass
class LLMAssignment:
    """单个文件的 LLM 分类结果。"""

    name: str
    category: tuple[str, ...] | None  # None 表示 unknown / 不在 taxonomy 中
    confidence: float
    reason: str


@dataclass
class LLMBatchResult:
    """一批文件的 LLM 分类结果。"""

    assignments: list[LLMAssignment] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 同义词表（需求 7.10）
# ---------------------------------------------------------------------------

#: 相近类目名归并表：每组内的名字视为同义，统一用第一个作为代表。
SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("发票", "发票单", "电子发票", "票据", "invoice"),
    ("合同", "合约", "协议", "contract", "agreement"),
    ("简历", "履历", "resume", "cv"),
    ("报销", "费用", "expense"),
    ("身份证", "身份证复印件", "身份证扫描件"),
    ("护照", "护照复印件", "护照扫描件"),
    ("图片", "图像", "照片", "image", "photo"),
    ("视频", "影片", "video"),
    ("音频", "音乐", "audio", "music"),
    ("文档", "文件", "document"),
    ("表格", "电子表格", "sheet"),
    ("演示", "幻灯片", "presentation"),
    ("代码", "源码", "code"),
    ("压缩包", "压缩文件", "archive"),
    ("安装程序", "安装包", "installer"),
)

#: 归并后的名字 -> 代表名
_SYNONYM_MAP: dict[str, str] = {}
for _group in SYNONYM_GROUPS:
    _representative = _group[0]
    for _name in _group:
        _SYNONYM_MAP[_name.lower()] = _representative


def _normalize_name(name: str) -> str:
    """归一化类目名：去空白、统一大小写。"""
    return re.sub(r"\s+", "", name).strip().lower()


def _synonym_of(name: str) -> str:
    """查找同义词代表名。找不到就返回原名。"""
    return _SYNONYM_MAP.get(_normalize_name(name), name)


def _levenshtein(a: str, b: str) -> int:
    """编辑距离。"""
    if a == b:
        return 0
    n, m = len(a), len(b)
    if n < m:
        a, b = b, a
        n, m = m, n
    if m == 0:
        return n
    prev = list(range(m + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(
                min(curr[-1] + 1, prev[j] + 1, prev[j - 1] + (ca != cb))
            )
        prev = curr
    return prev[m]


def _similar(a: str, b: str, threshold: int = 2) -> bool:
    """两个类目名是否相近（编辑距离 <= threshold 或互为同义词）。

    阈值是相对的：短名字（<=2 字）要求编辑距离 <= 1，长名字允许 threshold。
    这是为了避免「发票」和「合同」这种完全不同的 2 字词被误判为相近。
    """
    na, nb = _normalize_name(a), _normalize_name(b)
    if na == nb:
        return True
    if _synonym_of(a) == _synonym_of(b):
        return True
    dist = _levenshtein(na, nb)
    # 任一名字 <= 2 字时，只允许编辑距离 1
    if len(na) <= 2 or len(nb) <= 2:
        return dist <= 1
    return dist <= threshold


def merge_similar_categories(
    categories: list[tuple[str, ...]],
) -> list[tuple[str, ...]]:
    """归并相近类目名。需求 7.10。

    用并查集把编辑距离 <= 2 或互为同义词的类目名归到一组，每组用代表名。
    归并函数必须幂等：对已经归并过的列表再次调用结果不变。
    """
    if not categories:
        return []

    # 展平为段级比较：只比较最后一段（最具体的段）
    normalized = [_synonym_of(cat[-1]) for cat in categories]

    # 并查集
    parent = list(range(len(categories)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(len(categories)):
        for j in range(i + 1, len(categories)):
            if _similar(categories[i][-1], categories[j][-1]):
                union(i, j)

    # 收集归并后的类目
    groups: dict[int, tuple[str, ...]] = {}
    for i, cat in enumerate(categories):
        root = find(i)
        if root not in groups:
            groups[root] = cat  # 用第一个遇到的作为代表

    result = list(groups.values())
    # 去重（归并后可能产生重复）
    seen: set[tuple[str, ...]] = set()
    unique: list[tuple[str, ...]] = []
    for cat in result:
        if cat not in seen:
            seen.add(cat)
            unique.append(cat)
    return unique


def enforce_constraints(
    categories: list[tuple[str, ...]],
    max_count: int = MAX_CATEGORIES,
    max_depth: int = MAX_DEPTH,
) -> list[tuple[str, ...]]:
    """强制类目数与层级约束。需求 7.6、7.11。

    1. 截断层级深度到 max_depth。
    2. 归并相近类目。
    3. 仍超 max_count 则按文件数从少到多合并进 ``其他``。
    """
    # 1. 截断深度
    trimmed = [cat[:max_depth] for cat in categories]
    # 去重
    seen: set[tuple[str, ...]] = set()
    unique: list[tuple[str, ...]] = []
    for cat in trimmed:
        if cat and cat not in seen:
            seen.add(cat)
            unique.append(cat)

    # 2. 归并相近类目
    merged = merge_similar_categories(unique)

    # 3. 仍超则合并进 ``其他``
    if len(merged) <= max_count:
        return merged

    # 按类目名字典序稳定排序后，保留前 max_count - 1 个，其余并进 ``其他``
    merged.sort(key=lambda c: "/".join(c))
    keep = merged[: max_count - 1]
    return keep


def _build_taxonomy_prompt(summaries: Sequence[FileSummary]) -> str:
    """构建第一阶段提示：请求模型产出 taxonomy。"""
    lines = [f"- {s.name} ({s.ext}, {s.size}B)" for s in summaries]
    return (
        "你是一个文档分类专家。根据以下文件列表，设计一个分类体系。\n"
        "要求：\n"
        f"1. 类目数不超过 {MAX_CATEGORIES} 个。\n"
        f"2. 层级深度不超过 {MAX_DEPTH} 级。\n"
        "3. 类目名简洁、互斥、覆盖常见文档类型。\n"
        "4. 只输出 JSON 数组，每个元素是类目路径数组。\n"
        "示例：[[\"财务\", \"发票\"], [\"合同协议\"], [\"图片\"]]\n\n"
        "文件列表：\n" + "\n".join(lines)
    )


def _build_classify_prompt(
    summaries: Sequence[FileSummary],
    taxonomy: Taxonomy,
) -> str:
    """构建第二阶段提示：按固定 taxonomy 分类。"""
    tax_lines = ["- " + "/".join(cat) for cat in taxonomy.categories]
    file_lines = [f"- {s.name} ({s.ext})" for s in summaries]
    return (
        "你是一个文档分类专家。请把下列文件分类到给定的类目表中。\n"
        "要求：\n"
        "1. 每个文件只能分配到类目表中的一个类目，或返回 unknown。\n"
        "2. 只输出 JSON 数组，每个元素是 {\"name\": 文件名, \"category\": 类目路径数组或 null, \"reason\": 简要理由}。\n\n"
        "类目表：\n" + "\n".join(tax_lines) + "\n\n文件列表：\n" + "\n".join(file_lines)
    )


def _parse_taxonomy_json(text: str) -> Taxonomy:
    """从 LLM 响应中解析 taxonomy。"""
    # 尝试提取 JSON 数组
    text = text.strip()
    # 去掉 ```json 包裹
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 尝试从文本中提取方括号内容
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            data = json.loads(match.group())
        else:
            return Taxonomy()

    if not isinstance(data, list):
        return Taxonomy()

    categories: list[tuple[str, ...]] = []
    for item in data:
        if isinstance(item, str):
            categories.append((item,))
        elif isinstance(item, list) and all(isinstance(s, str) for s in item):
            categories.append(tuple(item))
    return Taxonomy(categories=categories)


def _parse_assignments_json(text: str) -> list[dict[str, Any]]:
    """从 LLM 响应中解析分类结果。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            data = json.loads(match.group())
        else:
            return []

    if not isinstance(data, list):
        return []
    return data


class TaxonomyBuilder:
    """两阶段 taxonomy 调用与收敛。需求 7.5-7.11。"""

    def __init__(
        self,
        provider: Provider,
        max_categories: int = MAX_CATEGORIES,
        max_depth: int = MAX_DEPTH,
        sample_size: int = MAX_SAMPLE_SIZE,
        batch_size: int = 100,
        timeout_seconds: float = 30.0,
        rng: random.Random | None = None,
    ) -> None:
        self._provider = provider
        self._max_categories = max_categories
        self._max_depth = max_depth
        self._sample_size = sample_size
        self._batch_size = max(MIN_BATCH_SIZE, min(MAX_BATCH_SIZE, batch_size))
        self._timeout = timeout_seconds
        self._rng = rng or random.Random()

    def build_taxonomy(self, summaries: Sequence[FileSummary]) -> Taxonomy:
        """第一阶段：抽样并请求模型产出 taxonomy。需求 7.5、7.6。"""
        if not summaries:
            return Taxonomy()

        # 抽样
        if len(summaries) <= self._sample_size:
            sample = list(summaries)
        else:
            sample = self._rng.sample(list(summaries), self._sample_size)

        prompt = _build_taxonomy_prompt(sample)
        request = ChatRequest(
            messages=(
                ChatMessage("system", "你是一个文档分类专家。只输出 JSON。"),
                ChatMessage("user", prompt),
            ),
            temperature=0.0,
        )

        try:
            response = self._provider.chat(request, timeout_seconds=self._timeout)
            raw_taxonomy = _parse_taxonomy_json(response.content)
        except (ProviderError, json.JSONDecodeError):
            # 失败时返回空 taxonomy，后续会回落到规则引擎
            return Taxonomy()

        # 强制约束
        constrained = enforce_constraints(
            raw_taxonomy.categories,
            max_count=self._max_categories,
            max_depth=self._max_depth,
        )
        return Taxonomy(categories=constrained)

    def classify_batch(
        self,
        summaries: Sequence[FileSummary],
        taxonomy: Taxonomy,
    ) -> LLMBatchResult:
        """第二阶段：按固定 taxonomy 分类一批文件。需求 7.7、7.9。"""
        if not summaries or not taxonomy.categories:
            return LLMBatchResult()

        prompt = _build_classify_prompt(summaries, taxonomy)
        request = ChatRequest(
            messages=(
                ChatMessage("system", "你是一个文档分类专家。只输出 JSON。"),
                ChatMessage("user", prompt),
            ),
            temperature=0.0,
        )

        try:
            response = self._provider.chat(request, timeout_seconds=self._timeout)
            raw = _parse_assignments_json(response.content)
        except (ProviderError, json.JSONDecodeError):
            return LLMBatchResult()

        assignments: list[LLMAssignment] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", ""))
            cat_raw = item.get("category")
            reason = str(item.get("reason", ""))

            if cat_raw is None:
                assignments.append(LLMAssignment(name=name, category=None, confidence=0.0, reason=reason))
                continue

            if isinstance(cat_raw, list) and all(isinstance(s, str) for s in cat_raw):
                cat = tuple(cat_raw)
            elif isinstance(cat_raw, str):
                cat = (cat_raw,)
            else:
                cat = None

            # 不在 taxonomy 中则 confidence 置 0（需求 7.9）
            if cat is not None and not taxonomy.has_category(cat):
                cat = None

            confidence = 0.8 if cat is not None else 0.0
            assignments.append(LLMAssignment(name=name, category=cat, confidence=confidence, reason=reason))

        return LLMBatchResult(assignments=assignments)

    def classify_all(
        self,
        summaries: Sequence[FileSummary],
        taxonomy: Taxonomy,
    ) -> LLMBatchResult:
        """分批发送全部文件。需求 7.7。"""
        all_summaries = list(summaries)
        result = LLMBatchResult()

        for i in range(0, len(all_summaries), self._batch_size):
            batch = all_summaries[i : i + self._batch_size]
            batch_result = self.classify_batch(batch, taxonomy)
            result.assignments.extend(batch_result.assignments)

        return result

    def estimate_request_count(self, total_files: int) -> int:
        """预估请求次数。需求 8.6。"""
        if total_files == 0:
            return 0
        # 第一阶段 1 次 + 第二阶段批次数
        phase2_batches = (total_files + self._batch_size - 1) // self._batch_size
        return 1 + phase2_batches


# ---------------------------------------------------------------------------
# 隐私分档（需求 8.1、8.2、8.3）
# ---------------------------------------------------------------------------

HEAD_CHARS = 500  # 元数据+正文前 500 字档的正文上限


def _make_relative_path(path: Path, root: Path) -> str:
    """把绝对路径转成相对路径（去掉根目录前缀）。需求 8.4。"""
    try:
        return str(Path(path).resolve().relative_to(root.resolve()))
    except ValueError:
        return path.name


def make_summary(
    entry: FileEntry,
    root: Path,
    privacy: PrivacyLevel,
) -> FileSummary:
    """由 FileEntry 构造符合隐私档的摘要。需求 8.1、8.2、8.4。

    - METADATA_ONLY：只发送文件名、扩展名、大小、修改时间、相对路径。
    - METADATA_PLUS_HEAD500：额外发送 text_head 的前 500 字符。
    - 绝对路径一律替换为相对路径。
    """
    text_head: str | None = None
    if privacy == PrivacyLevel.METADATA_PLUS_HEAD500 and entry.text_head:
        text_head = entry.text_head[:HEAD_CHARS]

    return FileSummary(
        name=entry.name,
        ext=entry.ext,
        size=entry.size,
        mtime=str(int(entry.mtime)),
        rel_path=_make_relative_path(entry.path, root),
        text_head=text_head,
    )


def make_summaries(
    entries: Sequence[FileEntry],
    root: Path,
    privacy: PrivacyLevel,
) -> list[FileSummary]:
    """批量构造摘要。"""
    return [make_summary(e, root, privacy) for e in entries]
