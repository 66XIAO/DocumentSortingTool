"""把 Provider / TaxonomyBuilder / LLMCache 串成一次完整的 LLM 分类。

## 为什么要有这一层

``LLMClassifier`` 刻意不发请求，它只查 ``ClassifyContext.llm_suggestions`` 这张表
（见 ``classifiers/by_llm.py``）。那张表得有人算出来，而算它需要网络调用、缓存、
分批、失败归类——这些都不该塞进 Planner 的五步流水线里，否则「分类求值」就不再是
确定的纯计算了（需求 3.13）。

于是分工是：
    本模块         负责一次性算出 path -> Suggestion 的快照（有 I/O，可失败）
    LLMClassifier  负责在管线里查表（无 I/O，确定）

## 失败一律回落，不向上抛

需求 6.5、6.6 要求任何 LLM 失败都只记日志、改用规则引擎结果，并且方案仍要覆盖
全部 FileEntry。所以 ``run()`` 不抛 ``ProviderError``：它返回一个可能为空的快照，
外加一句给用户看的 ``failure`` 说明。空快照会让管线里的 ``LLMClassifier`` 全部返回
None，自然退化成 filename → content → extension。

## 结果回填按相对路径优先

模型返回的是文件摘要里的 ``name`` 与 ``rel_path``。只按 name 匹配会在同名文件上
把类目挂到错误的文件身上，因此优先按 ``rel_path`` 匹配；模型没回显路径时，只有
「该 name 在本批内唯一」才采用，否则宁可放弃这条建议交给后续分类器
（需求 7.9 的精神：拿不准就交给下一个分类器）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.core.classifiers.base import SOURCE_LLM, Suggestion
from app.core.llm.cache import LLMCache
from app.core.llm.provider import Provider, ProviderError, make_provider
from app.core.llm.taxonomy import (
    FileSummary,
    LLMBatchResult,
    Taxonomy,
    TaxonomyBuilder,
    make_summaries,
)
from app.core.models import AIOptions, FileEntry, ProviderKind
from app.core.progress import CancelToken, NullCancelToken

logger = logging.getLogger(__name__)

#: 需求 6.5 指定的用户可见提示语。
FALLBACK_MESSAGE = "AI 分类不可用，已使用规则分类"

#: 五类失败的可读说明。需求 6.5。
_FAILURE_LABELS = {
    "timeout": "请求超时",
    "non_2xx": "服务返回错误状态",
    "schema": "返回内容不符合约定格式",
    "quota_exhausted": "额度耗尽或凭据无效",
    "network": "网络连接失败",
}


@dataclass
class LLMRunResult:
    """一次 LLM 分类的产出。"""

    #: 路径字符串 -> Suggestion。可为空（失败或全部 unknown）
    suggestions: dict[str, Suggestion] = field(default_factory=dict)
    #: 实际发出的请求次数（缓存命中的批次不计入）
    requests: int = 0
    #: 缓存命中的批次数
    cache_hits: int = 0
    #: 非空表示发生了回落，内容可直接展示给用户（需求 6.5）
    failure: str = ""
    #: 第一阶段产出的 taxonomy，供 UI 展示
    taxonomy: Taxonomy = field(default_factory=Taxonomy)
    #: 是否被用户取消
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return not self.failure and not self.cancelled


def estimate_requests(total_files: int, batch_size: int) -> int:
    """预估请求次数：1 次 taxonomy + 若干次分类。需求 8.6。"""
    if total_files <= 0:
        return 0
    size = max(1, batch_size)
    return 1 + (total_files + size - 1) // size


def provider_configured(options: AIOptions, api_key: str = "") -> bool:
    """是否配置出了一个能真正发起调用的 Provider。需求 6.3。

    需求 6.3 只说「未配置任何可用 Provider 时把开关渲染为禁用」，没有规定判据。
    这里取「缺了它就一定调不通」的最小集合：两家都必须有 model；
    OpenAI 兼容服务还需要 base_url 与 api_key；Ollama 只需要 host（本地无鉴权）。

    刻意不做网络探测：开关的可用性不该取决于此刻服务是否在线，否则用户离线时
    连「把 AI 关掉」这个动作都会被界面挡住。真正的可达性由设置页的「测试连通性」
    负责（需求 7.4）。
    """
    if not options.model.strip():
        return False
    if options.provider == ProviderKind.OLLAMA:
        return bool(options.host.strip())
    return bool(options.base_url.strip()) and bool(api_key.strip())


def make_provider_for(options: AIOptions, api_key: str = "") -> Provider:
    """按 AIOptions 构造 Provider。api_key 只从调用方传入，不落进 options。"""
    return make_provider(
        str(options.provider),
        base_url=options.base_url,
        api_key=api_key,
        host=options.host,
        model=options.model,
    )


def cache_scope(options: AIOptions, taxonomy: Taxonomy) -> str:
    """缓存 key 的作用域：换模型 / 换 taxonomy / 换隐私档都必须换 key。

    不含 api_key——凭据不进缓存 key，换一把同服务的 key 不该让整批缓存失效。
    """
    endpoint = (
        options.host
        if options.provider == ProviderKind.OLLAMA
        else options.base_url
    )
    tax = "|".join("/".join(cat) for cat in taxonomy.categories)
    return "\x00".join(
        [
            str(options.provider),
            endpoint,
            options.model,
            str(options.privacy_level),
            tax,
        ]
    )


class LLMRunner:
    """一次性算出 LLM 建议快照。"""

    def __init__(
        self,
        provider: Provider,
        options: AIOptions,
        cache: LLMCache | None = None,
        builder: TaxonomyBuilder | None = None,
    ) -> None:
        self._provider = provider
        self._options = options
        self._cache = cache
        self._builder = builder or TaxonomyBuilder(
            provider=provider,
            max_categories=options.max_categories,
            max_depth=options.max_depth,
            sample_size=options.taxonomy_sample_size,
            batch_size=options.batch_size,
            timeout_seconds=float(options.timeout_seconds),
        )

    # -- 对外 -------------------------------------------------------------

    def run(
        self,
        entries: Sequence[FileEntry],
        root: Path,
        cancel: CancelToken | None = None,
    ) -> LLMRunResult:
        """跑完两阶段调用并回填成 Suggestion 快照。

        任何 ``ProviderError`` 都被吞掉并转成 ``failure``，因为需求 6.6 要求方案
        必须照常产出。
        """
        if cancel is None:
            cancel = NullCancelToken()
        if not entries:
            return LLMRunResult()

        summaries = make_summaries(entries, root, self._options.privacy_level)
        by_rel_path, by_name = _index_entries(entries, summaries)

        if cancel.cancelled:
            return LLMRunResult(cancelled=True)

        # 第一阶段：taxonomy
        try:
            taxonomy = self._builder.build_taxonomy(summaries)
        except ProviderError as exc:
            return _failed(exc)

        if not taxonomy.categories:
            # build_taxonomy 内部已把 ProviderError 转成空 taxonomy，走到这里说明
            # 要么调用失败、要么模型没给出可用类目。两种都得回落（需求 6.5）。
            logger.warning("LLM 未产出可用 taxonomy，回落到规则分类")
            return LLMRunResult(
                requests=1,
                failure=f"{FALLBACK_MESSAGE}（模型未返回可用类目表）",
            )

        if cancel.cancelled:
            return LLMRunResult(taxonomy=taxonomy, requests=1, cancelled=True)

        # 第二阶段：分批 + 缓存
        scope = cache_scope(self._options, taxonomy)
        batch_size = max(1, self._options.batch_size)
        result = LLMRunResult(taxonomy=taxonomy, requests=1)

        for start in range(0, len(summaries), batch_size):
            if cancel.cancelled:
                result.cancelled = True
                return result

            batch = summaries[start : start + batch_size]
            batch_result = self._classify_batch(batch, taxonomy, scope, result)
            if batch_result is None:
                return result  # 已在 _classify_batch 里写入 failure

            _merge_assignments(
                batch_result, batch, by_rel_path, by_name, result.suggestions
            )

        return result

    # -- 内部 -------------------------------------------------------------

    def _classify_batch(
        self,
        batch: Sequence[FileSummary],
        taxonomy: Taxonomy,
        scope: str,
        result: LLMRunResult,
    ) -> LLMBatchResult | None:
        """一批的缓存查询与调用。失败时写入 ``result.failure`` 并返回 None。"""
        if self._cache is not None:
            cached = self._cache.get(batch, scope=scope)
            if cached is not None:
                result.cache_hits += 1
                return cached

        try:
            batch_result = self._builder.classify_batch(batch, taxonomy)
        except ProviderError as exc:
            logger.warning("LLM 分类批次失败: %s", exc)
            result.failure = f"{FALLBACK_MESSAGE}（{_describe(exc)}）"
            return None

        result.requests += 1
        if self._cache is not None:
            self._cache.put(batch, batch_result, scope=scope)
        return batch_result


# ---------------------------------------------------------------------------
# 回填
# ---------------------------------------------------------------------------


def _index_entries(
    entries: Sequence[FileEntry], summaries: Sequence[FileSummary]
) -> tuple[dict[str, FileEntry], dict[str, list[FileEntry]]]:
    """建立 rel_path -> entry 与 name -> [entry] 两张索引。

    ``make_summaries`` 与 ``entries`` 一一对应且同序，因此可以按下标配对。
    name 索引存列表而不是单值：重名必须能被识别出来，否则就会静默错配。
    """
    by_rel_path: dict[str, FileEntry] = {}
    by_name: dict[str, list[FileEntry]] = {}
    for entry, summary in zip(entries, summaries):
        by_rel_path.setdefault(summary.rel_path, entry)
        by_name.setdefault(summary.name, []).append(entry)
    return by_rel_path, by_name


def _merge_assignments(
    batch_result: LLMBatchResult,
    batch: Sequence[FileSummary],
    by_rel_path: dict[str, FileEntry],
    by_name: dict[str, list[FileEntry]],
    out: dict[str, Suggestion],
) -> None:
    """把一批 assignment 回填成 Suggestion。"""
    batch_paths = {s.rel_path for s in batch}

    for assignment in batch_result.assignments:
        # 需求 7.9：不在 taxonomy 中的类目 confidence 已被置 0 且 category 为 None，
        # 这里直接跳过，交给后续 Classifier
        if assignment.category is None or assignment.confidence <= 0:
            continue

        entry = _resolve_entry(assignment, batch_paths, by_rel_path, by_name)
        if entry is None:
            continue

        reason = assignment.reason.strip() or "AI 判断"
        out[str(entry.path)] = Suggestion(
            category=assignment.category,
            confidence=assignment.confidence,
            reason=f"AI：{reason}",
            source=SOURCE_LLM,
        )


def _resolve_entry(
    assignment: object,
    batch_paths: set[str],
    by_rel_path: dict[str, FileEntry],
    by_name: dict[str, list[FileEntry]],
) -> FileEntry | None:
    """把一条 assignment 对应回唯一的 FileEntry，拿不准就返回 None。"""
    rel_path = getattr(assignment, "rel_path", "") or ""
    name = getattr(assignment, "name", "") or ""

    # 首选相对路径，且必须属于本批——模型偶尔会回显别批甚至臆造的路径
    if rel_path and rel_path in batch_paths:
        entry = by_rel_path.get(rel_path)
        if entry is not None:
            return entry

    # 退回名字，只有全局唯一才敢用
    candidates = by_name.get(name, [])
    if len(candidates) == 1:
        return candidates[0]

    if candidates:
        logger.debug("LLM 结果按名字无法消歧，已跳过: %s", name)
    return None


def _describe(exc: ProviderError) -> str:
    return _FAILURE_LABELS.get(exc.kind, exc.kind)


def _failed(exc: ProviderError) -> LLMRunResult:
    logger.warning("LLM taxonomy 阶段失败: %s", exc)
    return LLMRunResult(failure=f"{FALLBACK_MESSAGE}（{_describe(exc)}）")
