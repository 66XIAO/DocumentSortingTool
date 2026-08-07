"""LLM 批级缓存。

需求 8.5、8.6、8.9：一批文件摘要的哈希已存在于本地 SQLite 缓存时，复用缓存结果并
跳过该批请求。缓存命中返回的类目分配必须与首次调用产出的一致（往返一致性）。

缓存 key 为文件摘要集合的哈希（SHA-256），使同一组文件无论顺序如何都能命中。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.models import to_jsonable
from app.core.llm.taxonomy import FileSummary, LLMBatchResult, LLMAssignment


@dataclass
class CacheStats:
    """缓存统计。"""

    hits: int = 0
    misses: int = 0
    inserts: int = 0


class LLMCache:
    """SQLite 批级缓存。需求 8.5。

    表结构：
        batch_cache(key TEXT PRIMARY KEY, created_at TEXT, result BLOB)
    """

    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._stats = CacheStats()
        self._init_db()

    def _init_db(self) -> None:
        """建表。"""
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS batch_cache (
                    key TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    result TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._path))

    @property
    def stats(self) -> CacheStats:
        return self._stats

    @staticmethod
    def make_key(summaries: Sequence[FileSummary]) -> str:
        """由文件摘要集合生成稳定哈希 key。

        排序后哈希，使不同顺序的同一组摘要得到相同 key。
        """
        # 用 (name, ext, size) 作为摘要的标识，排序后序列化
        items = sorted(
            ((s.name, s.ext, s.size) for s in summaries),
            key=lambda x: x[0],
        )
        data = json.dumps(items, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def get(self, summaries: Sequence[FileSummary]) -> LLMBatchResult | None:
        """查询缓存。需求 8.5。"""
        key = self.make_key(summaries)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT result FROM batch_cache WHERE key = ?", (key,)
            ).fetchone()

        if row is None:
            self._stats.misses += 1
            return None

        self._stats.hits += 1
        return _decode_result(json.loads(row[0]))

    def put(
        self,
        summaries: Sequence[FileSummary],
        result: LLMBatchResult,
    ) -> None:
        """写入缓存。需求 8.5。"""
        import datetime as _dt

        key = self.make_key(summaries)
        payload = json.dumps(_encode_result(result), ensure_ascii=False)
        now = _dt.datetime.now().isoformat()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO batch_cache (key, created_at, result)
                VALUES (?, ?, ?)
                """,
                (key, now, payload),
            )
        self._stats.inserts += 1

    def clear(self) -> None:
        """清空缓存。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM batch_cache")

    def close(self) -> None:
        """关闭（SQLite 不需要显式关闭连接，但提供此方法保持接口一致）。"""
        pass


def _encode_result(result: LLMBatchResult) -> dict[str, Any]:
    """序列化结果。复用 to_jsonable 保持一致性。"""
    return {
        "assignments": [
            {
                "name": a.name,
                "category": list(a.category) if a.category else None,
                "confidence": a.confidence,
                "reason": a.reason,
            }
            for a in result.assignments
        ]
    }


def _decode_result(data: dict[str, Any]) -> LLMBatchResult:
    """反序列化结果。需求 8.9（往返一致性）。"""
    assignments: list[LLMAssignment] = []
    for item in data.get("assignments", []):
        cat_raw = item.get("category")
        category = tuple(cat_raw) if isinstance(cat_raw, list) else None
        assignments.append(
            LLMAssignment(
                name=item.get("name", ""),
                category=category,
                confidence=float(item.get("confidence", 0.0)),
                reason=item.get("reason", ""),
            )
        )
    return LLMBatchResult(assignments=assignments)
