"""按修改时间分类。需求 3.4。

只依赖 ``FileEntry.mtime``，不读时钟——这是确定性（需求 3.13）的前提之一：
读「今天」会让同一份输入在不同日期产出不同结果。
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.classifiers.base import SOURCE_DATE, ClassifyContext, Suggestion
from app.core.models import DateGranularity, FileEntry

#: mtime 必然存在，因此置信度为 1
CONFIDENCE = 1.0

#: 不参与 priority 竞争，见 base 模块的说明
PRIORITY = 0


class DateClassifier:
    """时间类目。"""

    name = SOURCE_DATE
    priority = PRIORITY

    def classify(self, entry: FileEntry, ctx: ClassifyContext) -> Suggestion | None:
        parts = date_parts(entry.mtime, ctx.options.date_granularity)
        if not parts:
            return None
        return Suggestion(
            category=parts,
            confidence=CONFIDENCE,
            reason=f"修改时间 {_stamp(entry.mtime)} → {'/'.join(parts)}",
            source=SOURCE_DATE,
        )


def date_parts(mtime: float, granularity: DateGranularity) -> tuple[str, ...]:
    """时间类目路径。

    ``YEAR`` → ``("2024",)``；``YEAR_MONTH`` → ``("2024", "2024-03")``。
    第二级带上年份前缀，这样把目录单独拷出去也不会只剩一个 ``03`` 让人猜。
    """
    moment = _to_datetime(mtime)
    year = f"{moment.year:04d}"
    if granularity is DateGranularity.YEAR:
        return (year,)
    return (year, f"{year}-{moment.month:02d}")


def _to_datetime(mtime: float) -> datetime:
    """用本地时区解释 mtime。

    文件时间对用户的意义是「我那天存的」，本地时区才符合直觉。异常取值
    （负数、超出范围）回落到 Unix 纪元，避免抛异常打断整批分类。
    """
    try:
        return datetime.fromtimestamp(mtime)
    except (OverflowError, OSError, ValueError):
        return datetime.fromtimestamp(0, tz=timezone.utc).replace(tzinfo=None)


def _stamp(mtime: float) -> str:
    return _to_datetime(mtime).strftime("%Y-%m-%d")
