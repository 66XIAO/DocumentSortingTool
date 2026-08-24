"""全部领域模型、枚举与统一的 JSON 编解码器。

本模块是 core 层的地基，零 Qt 依赖、零 app.config 依赖。

## 为什么编解码器只有一对函数

设计里有六处需要序列化往返，且每一处都有对应的往返一致性验收标准：

    rules.yaml      需求 4.5
    journal.jsonl   需求 13.9
    manifest.json   需求 13.10
    settings.yaml   需求 18.3
    LLM 批级缓存    需求 8.9
    OverrideSet     需求 19.12

如果每处各写一遍 Path/枚举/元组的转换规则，往返就会在某个角落静默失效——而
失效的后果是撤销日志读不回来，也就是用户的文件救不回来。因此这里只提供
``to_jsonable`` / ``from_jsonable`` 一对函数，六处共用，靠类型注解驱动解码。

## 类目路径为什么是元组而不是字符串

``Category.path_parts`` 用 ``tuple[str, ...]`` 而非 ``"财务/发票"`` 字符串：
类目名本身可能含分隔符，元组表示让「清洗非法字符」只作用于单个段，也让序列化
不受分隔符歧义影响。相应地，以元组为 key 的字典不能编码成 JSON 对象（JSON 的
key 必须是字符串，拼接就会重新引入歧义），本模块把它编码成键值对列表。
"""

from __future__ import annotations

import dataclasses
import hashlib
import types
import typing
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePath
from typing import Any, Literal, get_args, get_origin, get_type_hints

# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class ScanScope(StrEnum):
    """扫描范围。需求 2.1，默认 TOP_LEVEL_ONLY。"""

    TOP_LEVEL_ONLY = "top_level_only"
    SELECTED_SUBFOLDERS = "selected_subfolders"


class Strategy(StrEnum):
    """分类策略。需求 3.1，默认 BY_TYPE（需求 3.2）。"""

    BY_TYPE = "by_type"
    BY_DATE = "by_date"
    TYPE_AND_DATE = "type_and_date"
    SMART = "smart"


class DateGranularity(StrEnum):
    """时间类目粒度。需求 3.4。

    需求正文用「年」「年-月」描述取值，这里用 ASCII 作为配置与序列化的取值，
    界面上仍显示中文。理由是配置文件与 journal 里避免非 ASCII 取值，减少编码
    相关的边界问题；语义与需求一致。
    """

    YEAR = "year"
    YEAR_MONTH = "year_month"


class ActionKind(StrEnum):
    """单个条目的处置动作。需求 9.2。"""

    MOVE = "move"
    COPY = "copy"
    SKIP = "skip"


class ConflictKind(StrEnum):
    """冲突态。取值域封闭且完备，需求 9.3。

    PATH_ESCAPE 的唯一来源是 SafetyGuard.check_target 判定失败（需求 1.5）。
    """

    NONE = "none"
    EXISTS = "exists"
    PATH_TOO_LONG = "path_too_long"
    LOCKED = "locked"
    PATH_ESCAPE = "path_escape"


class ConflictPolicy(StrEnum):
    """同名冲突策略。需求 9.4-9.6，默认 AUTO_RENAME。"""

    AUTO_RENAME = "auto_rename"
    SKIP = "skip"
    OVERWRITE = "overwrite"


class PrivacyLevel(StrEnum):
    """LLM 请求的隐私档。需求 8.1、8.2，默认 METADATA_ONLY。"""

    METADATA_ONLY = "metadata_only"
    METADATA_PLUS_HEAD500 = "metadata_plus_head500"


class ProviderKind(StrEnum):
    """LLM 服务适配层的实现选择。需求 7.1。"""

    OPENAI_COMPAT = "openai_compat"
    OLLAMA = "ollama"


class RecordKind(StrEnum):
    """journal 记录类型。

    INTENT / DONE / FAILED 覆盖单个文件操作的前后两写（需求 13.3、13.4）。
    SKIPPED 记录 action=skip 或 included=false 而未执行的条目，使结果报告的三组
    计数之和等于待处理条目数（属性 26）。

    CREATED_DIR 与 REMOVED_DIR 的撤销语义**相反**，绝不可混用（需求 20.19）：
        CREATED_DIR 是执行时新建的类目目录 -> 撤销时删除（且仅当为空）
        REMOVED_DIR 是执行时清理掉的空目录 -> 撤销时重建

    TRASHED 记录覆盖策略下被移入回收站的目标文件（需求 12.4），使撤销能向用户
    说明「哪个文件被覆盖了、去哪儿找」。
    """

    INTENT = "intent"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    CREATED_DIR = "created_dir"
    REMOVED_DIR = "removed_dir"
    TRASHED = "trashed"


class Outcome(StrEnum):
    """执行结果分组。需求 16.2 的三组。"""

    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"


class RunStatus(StrEnum):
    """run 的生命周期状态。需求 15.1、15.6。

    UNFINISHED 表示存在 intent 但缺少对应 done/failed，即未收尾（需求 13.7）。
    """

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    UNDONE = "undone"
    UNFINISHED = "unfinished"


class RootVerdict(StrEnum):
    """根目录准入判定。需求 1.1。"""

    ALLOW = "allow"
    REJECT = "reject"


class RejectReason(StrEnum):
    """准入拒绝原因码。封闭集合，需求 1.2、1.3。

    属性 1 要求「返回 reject 时必定附带一个属于封闭原因码集合的原因」，因此
    这个枚举的成员就是那个封闭集合，SafetyGuard 不得返回集合外的原因。
    """

    DRIVE_ROOT = "drive_root"
    SYSTEM_PATH = "system_path"
    NOT_EXISTS = "not_exists"
    NOT_A_DIR = "not_a_dir"
    NO_PERMISSION = "no_permission"


# ---------------------------------------------------------------------------
# 注入 core 的窄选项对象
#
# core 不 import app.config（需求 18.7）。它需要的配置以下面这些窄对象注入，
# 由 config 层从 Settings 构造，因此 config -> core 是单向依赖，不成环。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanOptions:
    """扫描选项。

    刻意**不含** ``scope`` 字段：扫描范围是 ``ScanSelection.scope()`` 的派生值，
    放进选项对象就有了两个事实来源，一旦不同步会得到「选项说 top_level_only、
    勾选集合却非空」这种自相矛盾的状态。

    ``excluded_names`` 按名字排除（大小写不敏感），默认排除我们自己的元数据目录
    ``.docsort``（需求 2.19、13.5）。
    """

    include_hidden: bool = False
    follow_symlinks: bool = False
    excluded_names: frozenset[str] = frozenset({".docsort"})


@dataclass(frozen=True)
class ClassifyOptions:
    strategy: Strategy = Strategy.BY_TYPE
    min_confidence: float = 0.6
    date_granularity: DateGranularity = DateGranularity.YEAR_MONTH
    merge_small_categories: bool = False
    small_category_threshold: int = 3


@dataclass(frozen=True)
class ExecOptions:
    """执行语义选项。

    ``default_action`` 是在设计给出的四个字段之外补的：需求 11.1 要求默认动作为
    「移动」，而这个取值在规划阶段就要写进 ``PlanItem.action``，Planner 需要读到它。
    """

    dry_run: bool = False
    conflict_policy: ConflictPolicy = ConflictPolicy.AUTO_RENAME
    remove_empty_dirs: bool = False
    verify_hash: bool = True
    default_action: ActionKind = ActionKind.MOVE


@dataclass(frozen=True)
class AIOptions:
    """AI 分类的窄选项对象。

    ``host`` 是 ``OllamaProvider`` 的必需参数（需求 7.1），必须和 ``base_url``
    并列存在：两个 Provider 的连接参数不同名，合并成一个字段会让「切换 Provider
    后连到上一个服务的地址」成为可能。

    刻意**不含** api_key：凭据只从 keyring 取（需求 7.2、7.3），让它进入一个会被
    序列化、被日志打印的选项对象，就等于给明文泄漏开了口子。
    """

    enabled: bool = False
    provider: ProviderKind = ProviderKind.OPENAI_COMPAT
    base_url: str = ""
    host: str = ""
    model: str = ""
    timeout_seconds: int = 30
    privacy_level: PrivacyLevel = PrivacyLevel.METADATA_ONLY
    taxonomy_sample_size: int = 300
    max_categories: int = 12
    max_depth: int = 2
    batch_size: int = 100


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------


@dataclass
class FileEntry:
    """单个文件的元数据。需求 2.2。

    depth 相对根目录计算：根目录下的孤立文件为 1。
    error 非空表示该条目读取失败，但不中断整体扫描（需求 2.16）。
    """

    path: Path
    name: str
    ext: str  # 小写，含前导点；无扩展名时为空串
    size: int
    mtime: float
    is_hidden: bool
    depth: int
    mime: str | None = None
    text_head: str | None = None
    error: str | None = None


@dataclass
class SubfolderInfo:
    """子文件夹清单项。需求 2.4。

    统计三个数值只读元数据，被统计到的文件不进入 FileEntry 集合（需求 2.5）。
    has_children 供树形控件决定是否显示展开箭头，避免为此再遍历一次。
    """

    path: Path
    name: str
    loose_file_count: int
    recursive_file_count: int
    recursive_size: int
    depth: int
    has_children: bool
    selected: bool = False


@dataclass
class ScanSelection:
    """扫描范围的选择状态。

    勾选不向下继承：勾选 S 只让 S 的孤立文件参与整理，S 的子文件夹仍需单独
    勾选（需求 2.7）。这条语义是「目录结构默认不被改动」这一红线的来源。
    """

    root: Path
    selected: set[Path] = field(default_factory=set)

    def scope(self) -> ScanScope:
        """需求 2.9：勾选集合非空即为 SELECTED_SUBFOLDERS。"""
        return (
            ScanScope.SELECTED_SUBFOLDERS
            if self.selected
            else ScanScope.TOP_LEVEL_ONLY
        )

    def selected_closure(self) -> frozenset[Path]:
        """空目录清理的候选范围上界。需求 20.6。

        因为勾选不继承，这个集合就等于勾选集合本身。根目录**不在**其中——
        需求 20.14 要求根目录在任何配置组合下都保留。
        """
        return frozenset(self.selected)

    def is_selected(self, path: Path) -> bool:
        return path in self.selected

    def sorted_selected(self) -> tuple[Path, ...]:
        """稳定顺序，供写入 manifest（需求 13.1）。"""
        return tuple(sorted(self.selected, key=str))


# ---------------------------------------------------------------------------
# 方案
# ---------------------------------------------------------------------------


@dataclass
class Category:
    """类目。

    path_parts 支持多级（如 ("财务", "发票")）。id 由 path_parts 派生，保证
    同一类目路径在多次重算之间得到同一 id，这样 override 才能稳定地按 id 挂靠。
    """

    id: str
    path_parts: tuple[str, ...]
    color: str
    rule_source: str  # "rule:fin_invoice" / "llm" / "date" / "extension" / "user"

    @staticmethod
    def make_id(path_parts: tuple[str, ...]) -> str:
        """由类目路径派生稳定 id。

        用 NUL 连接各段再取哈希：NUL 不可能出现在 Windows 文件名里，因此不存在
        ("a/b",) 与 ("a", "b") 撞同一个 id 的可能。若改用 "/" 连接就会撞。
        """
        joined = "\x00".join(path_parts)
        return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def create(
        cls,
        path_parts: tuple[str, ...],
        color: str,
        rule_source: str,
    ) -> Category:
        return cls(
            id=cls.make_id(path_parts),
            path_parts=path_parts,
            color=color,
            rule_source=rule_source,
        )


@dataclass
class PlanItem:
    """单个文件的处置决定。

    reason 与 confidence 是可解释性的载体：用户凭它们判断分类是否可信，这直接
    决定他敢不敢点执行（需求 3.10）。
    renamed_from 在自动重命名时记录旧名，供预览树显示「旧名 -> 新名」（需求 10.6）。
    """

    entry: FileEntry
    category_id: str
    target: Path
    action: ActionKind
    conflict: ConflictKind
    confidence: float
    reason: str
    included: bool = True
    renamed_from: str | None = None
    by_llm: bool = False  # 供预览树显示 AI 角标，需求 8.7


@dataclass(frozen=True)
class PlanStats:
    """预览页统计卡的数据。需求 10.1。"""

    total_files: int
    category_count: int
    pending_move: int
    conflict_count: int
    unclassified_count: int


@dataclass
class SortPlan:
    """一次整理的完整方案。需求 9.1。"""

    root: Path
    strategy: Strategy
    categories: list[Category]
    items: list[PlanItem]
    unclassified: list[PlanItem]
    scope: ScanScope = ScanScope.TOP_LEVEL_ONLY
    selected_subfolders: tuple[Path, ...] = ()

    def all_items(self) -> list[PlanItem]:
        """items 与 unclassified 的并集。

        属性 12 要求两者合起来覆盖全部 FileEntry 且不重复，这个方法是该断言的
        唯一取数入口，避免调用方各自拼接时漏掉一边。
        """
        return [*self.items, *self.unclassified]

    def stats(self) -> PlanStats:
        every = self.all_items()
        return PlanStats(
            total_files=len(every),
            category_count=len(self.categories),
            pending_move=sum(
                1
                for i in every
                if i.included and i.action is not ActionKind.SKIP
            ),
            conflict_count=sum(
                1 for i in every if i.conflict is not ConflictKind.NONE
            ),
            unclassified_count=len(self.unclassified),
        )

    def category_by_id(self, category_id: str) -> Category | None:
        for c in self.categories:
            if c.id == category_id:
                return c
        return None


# ---------------------------------------------------------------------------
# 用户覆盖层（override）
# ---------------------------------------------------------------------------


@dataclass
class ItemOverride:
    """单个条目的手工调整。

    key 是文件绝对路径（需求 19.2）。字段为 None 表示该维度未被覆盖，从而允许
    「只改类目不改勾选」这类部分覆盖，重算时未覆盖的维度仍由分类器决定。
    """

    category_path: tuple[str, ...] | None = None  # 拖拽改类目，需求 10.11
    included: bool | None = None  # 复选框排除，需求 10.10

    def is_empty(self) -> bool:
        return self.category_path is None and self.included is None


@dataclass
class CategoryOverride:
    """类目级手工调整。需求 10.2。

    key 是类目**原始**路径元组。renamed_to 为空表示未改名。
    merged_into 非空表示该类目被合并进另一个类目。
    deleted 为真表示用户删除了该类目，其下文件退回 `_未分类`（需求 10.3）。
    """

    renamed_to: tuple[str, ...] | None = None
    color: str | None = None
    merged_into: tuple[str, ...] | None = None
    deleted: bool = False
    created: bool = False


@dataclass
class OverrideSet:
    """用户覆盖层。

    override 是规划链的**独立输入**，不是对 SortPlan 的原地修改。这样重算时
    基础方案整体重建也不会冲掉手工调整——「保留手工调整」因此是结构上必然
    成立的，而不是靠调用方小心维护（需求 19.3、19.13、19.14）。
    """

    version: int = 1
    items: dict[str, ItemOverride] = field(default_factory=dict)
    categories: dict[tuple[str, ...], CategoryOverride] = field(
        default_factory=dict
    )

    def __len__(self) -> int:
        return len(self.items) + len(self.categories)

    def is_empty(self) -> bool:
        return not self.items and not self.categories

    def item_for(self, path: Path | str) -> ItemOverride | None:
        return self.items.get(str(path))

    def set_item(self, path: Path | str, override: ItemOverride) -> None:
        key = str(path)
        if override.is_empty():
            self.items.pop(key, None)
        else:
            self.items[key] = override

    def clear(self) -> None:
        self.items.clear()
        self.categories.clear()


# ---------------------------------------------------------------------------
# 日志与 run
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JournalRecord:
    """journal.jsonl 的单行记录。需求 13.9。

    frozen 是刻意的：记录一旦写出就不该被改动，撤销的正确性依赖它忠实反映
    执行时的事实。src/dst 存字符串绝对路径而非 Path，使 JSONL 行与内存对象
    一一对应，往返不引入歧义。
    """

    seq: int
    ts: str  # ISO-8601
    kind: RecordKind
    op: ActionKind | None = None
    src: str | None = None
    dst: str | None = None
    size: int | None = None
    mtime: float | None = None
    sha256: str | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceSnapshotEntry:
    """执行前源树快照的单条。需求 13.1。

    撤销前要用 size + mtime 与当前文件比对，不一致就拒绝覆盖（需求 14.6），
    快照是那次比对的基准。
    """

    path: str
    size: int
    mtime: float


@dataclass
class Manifest:
    """一次执行的完整现场。需求 13.1、19.11。

    除方案本身，还记录 scope 与勾选清单——否则撤销后重新生成方案时无法复现
    当时的扫描范围，幂等性断言（需求 9.13）就失去前提。
    """

    run_id: str
    created_at: str
    root: str
    strategy: Strategy
    scope: ScanScope
    selected_subfolders: tuple[str, ...]
    conflict_policy: ConflictPolicy
    remove_empty_dirs: bool
    plan: SortPlan
    source_snapshot: list[SourceSnapshotEntry]
    overrides: OverrideSet
    app_version: str


@dataclass
class RunMeta:
    """历史记录页的一行。需求 15.1。"""

    run_id: str
    started_at: str
    root: str
    strategy: Strategy
    file_count: int
    status: RunStatus
    finished_at: str | None = None
    undone_at: str | None = None

    def is_undoable(self) -> bool:
        """需求 15.2、15.6：已撤销的 run 不可再撤销。"""
        return self.status in (
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.UNFINISHED,
        )


# ---------------------------------------------------------------------------
# 执行报告
# ---------------------------------------------------------------------------

#: 类目 chip 的 10 色柔和色板。需求 17.8。
#
# 放在 core 而不是 ui/theme 里，因为 ``Category.color`` 是 core 的字段、要随
# manifest 落盘。ui/theme/tokens.py 从这里再导出，保持单一事实来源——core 不能
# import ui（需求 18.7），若两边各存一份，改了一边就会出现「界面上的颜色和
# manifest 里记的颜色不一致」。
CATEGORY_PALETTE: tuple[str, ...] = (
    "#3B82F6",
    "#10B981",
    "#F59E0B",
    "#8B5CF6",
    "#EC4899",
    "#06B6D4",
    "#84CC16",
    "#F97316",
    "#6366F1",
    "#14B8A6",
)

#: `_未分类` 固定用中性灰，不占色板名额——它不是一个「真」类目
UNCLASSIFIED_COLOR = "#94A3B8"

UNCLASSIFIED_PARTS: tuple[str, ...] = ("_未分类",)


def category_color(index: int) -> str:
    """按类目序号轮转取色。属性 41 要求 color(i) == color(i + 10)。"""
    return CATEGORY_PALETTE[index % len(CATEGORY_PALETTE)]


def color_for_category(parts: tuple[str, ...], index: int) -> str:
    """类目路径对应的颜色。`_未分类` 走中性灰。"""
    if parts == UNCLASSIFIED_PARTS:
        return UNCLASSIFIED_COLOR
    return category_color(index)


CSV_SRC = "源路径"
CSV_DST = "目标路径"
CSV_ACTION = "动作"
CSV_OUTCOME = "结果"
CSV_REASON = "理由"

CSV_FIELDNAMES: tuple[str, ...] = (
    CSV_SRC,
    CSV_DST,
    CSV_ACTION,
    CSV_OUTCOME,
    CSV_REASON,
)


@dataclass(frozen=True)
class ResultRow:
    """结果报告的一行，同时是 CSV 导出的一行。需求 16.4。"""

    src: str
    dst: str
    action: ActionKind
    outcome: Outcome
    reason: str

    def to_csv_row(self) -> dict[str, str]:
        return {
            CSV_SRC: self.src,
            CSV_DST: self.dst,
            CSV_ACTION: str(self.action),
            CSV_OUTCOME: str(self.outcome),
            CSV_REASON: self.reason,
        }


@dataclass
class ExecutionReport:
    """一次执行的结果。需求 16.2。

    predicted_removed_dirs 在模拟运行时填充（需求 20.20），与真实执行的
    removed_dirs 必须一致——两者由同一个纯函数算出（属性 38）。
    """

    succeeded: list[ResultRow] = field(default_factory=list)
    skipped: list[ResultRow] = field(default_factory=list)
    failed: list[ResultRow] = field(default_factory=list)
    removed_dirs: list[Path] = field(default_factory=list)
    predicted_removed_dirs: list[Path] = field(default_factory=list)
    needs_manual_review: list[ResultRow] = field(default_factory=list)
    elapsed_ms: int = 0
    dry_run: bool = False
    #: 用户中途点了「停止」。需求 11.7 要求停止后提供「回滚已完成部分」，UI 得先知道
    #: 这次是被停下来的——靠在 skipped 的 reason 里找字符串太脆，加一个字段更诚实。
    stopped: bool = False

    def to_csv_rows(self) -> list[dict[str, str]]:
        """三组按顺序拼接。

        需求 16.5 要求「CSV 行数 == 三组计数之和」。这里让它成为实现上的恒等式
        而不是需要另外校验的约定：只要不改这行拼接，断言就不可能失败。
        """
        return [
            row.to_csv_row()
            for row in (*self.succeeded, *self.skipped, *self.failed)
        ]

    def counts(self) -> dict[Outcome, int]:
        return {
            Outcome.SUCCEEDED: len(self.succeeded),
            Outcome.SKIPPED: len(self.skipped),
            Outcome.FAILED: len(self.failed),
        }

    def total(self) -> int:
        return len(self.succeeded) + len(self.skipped) + len(self.failed)


# ---------------------------------------------------------------------------
# 统一编解码器
# ---------------------------------------------------------------------------

_PASSTHROUGH = (bool, int, float, str)


def to_jsonable(value: Any) -> Any:
    """把领域对象编码成只含 JSON 原生类型的结构。

    转换规则（六处序列化共用）：
        dataclass          -> dict，字段顺序即声明顺序
        StrEnum            -> str
        Path               -> str
        tuple/list         -> list
        set/frozenset      -> list，按编码后的字符串排序以保证输出稳定
        dict（str key）    -> JSON 对象
        dict（非 str key） -> [[key, value], ...] 键值对列表

    最后一条是必须的：以类目元组为 key 的字典无法表示成 JSON 对象，而把元组
    拼成字符串会重新引入分隔符歧义（类目名本身可能含 "/"）。
    """
    if value is None:
        return None

    # StrEnum 同时是 str 的子类，必须在 _PASSTHROUGH 之前判断，否则会被当成
    # 普通字符串直接放过——那样解码时就拿不回枚举类型。
    if isinstance(value, StrEnum):
        return str(value)

    if isinstance(value, _PASSTHROUGH):
        return value

    if isinstance(value, PurePath):
        return str(value)

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: to_jsonable(getattr(value, f.name))
            for f in dataclasses.fields(value)
        }

    if isinstance(value, Mapping):
        if all(isinstance(k, str) and not isinstance(k, StrEnum) for k in value):
            return {k: to_jsonable(v) for k, v in value.items()}
        return [[to_jsonable(k), to_jsonable(v)] for k, v in value.items()]

    if isinstance(value, (set, frozenset)):
        encoded = [to_jsonable(v) for v in value]
        return sorted(encoded, key=_sort_key)

    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]

    raise TypeError(f"无法编码为 JSON 的类型: {type(value)!r}")


def _sort_key(encoded: Any) -> str:
    """集合元素的稳定排序键。

    集合无序，但落盘内容必须稳定，否则同一份数据两次写出的文件不同，diff 与
    缓存命中都会失真。
    """
    return repr(encoded)


def from_jsonable(data: Any, target: Any) -> Any:
    """按目标类型把 JSON 结构解码回领域对象。

    解码由类型注解驱动而非数据形态驱动——这是往返一致性能成立的关键：
    ``"move"`` 到底该变回 ``ActionKind.MOVE`` 还是留作字符串，只有目标类型
    知道答案。
    """
    return _decode(data, target)


def _decode(value: Any, tp: Any) -> Any:
    if tp is Any or tp is None or tp is type(None):
        return value

    origin = get_origin(tp)

    # Optional[X] 与 X | Y
    if origin is types.UnionType or origin is typing.Union:
        args = [a for a in get_args(tp) if a is not type(None)]
        if value is None:
            return None
        if len(args) == 1:
            return _decode(value, args[0])
        return _decode_first_match(value, args)

    if origin is Literal:
        return value

    if dataclasses.is_dataclass(tp) and isinstance(tp, type):
        return _decode_dataclass(value, tp)

    if isinstance(tp, type):
        if issubclass(tp, StrEnum):
            return tp(value)
        if issubclass(tp, PurePath):
            return Path(value)
        if issubclass(tp, bool):
            return bool(value)

    if origin is list:
        item_tp = _single_arg(tp)
        return [_decode(v, item_tp) for v in value]

    if origin is tuple:
        args = get_args(tp)
        if not args:
            return tuple(value)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(v, args[0]) for v in value)
        return tuple(_decode(v, a) for v, a in zip(value, args, strict=False))

    if origin in (set, frozenset):
        item_tp = _single_arg(tp)
        decoded = [_decode(v, item_tp) for v in value]
        return set(decoded) if origin is set else frozenset(decoded)

    if origin is dict:
        args = get_args(tp) or (Any, Any)
        key_tp, val_tp = args[0], args[1]
        if key_tp is str or key_tp is Any:
            return {k: _decode(v, val_tp) for k, v in value.items()}
        # 非 str key：编码形态是键值对列表
        return {_decode(k, key_tp): _decode(v, val_tp) for k, v in value}

    if isinstance(tp, type) and issubclass(tp, (int, float, str)):
        return tp(value)

    return value


def _single_arg(tp: Any) -> Any:
    args = get_args(tp)
    return args[0] if args else Any


def _decode_first_match(value: Any, candidates: list[Any]) -> Any:
    """联合类型：按声明顺序尝试，取第一个能解码的。

    本项目的联合都是 ``X | None`` 这种简单形态，走不到多候选分支；保留这条是
    为了让新增字段时不会静默拿到未解码的原始值。
    """
    for candidate in candidates:
        try:
            return _decode(value, candidate)
        except (TypeError, ValueError, KeyError):
            continue
    return value


def _decode_dataclass(value: Any, tp: type) -> Any:
    if not isinstance(value, Mapping):
        raise TypeError(f"{tp.__name__} 需要一个对象，实际得到 {type(value)!r}")

    hints = _resolved_hints(tp)
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(tp):
        if not f.init:
            continue
        if f.name not in value:
            # 缺字段交给 dataclass 的默认值；没有默认值时由构造函数报错，
            # 这正是我们想要的——静默填 None 会让损坏的 journal 看起来正常。
            continue
        kwargs[f.name] = _decode(value[f.name], hints.get(f.name, Any))
    return tp(**kwargs)


_HINT_CACHE: dict[type, dict[str, Any]] = {}


def _resolved_hints(tp: type) -> dict[str, Any]:
    """解析 dataclass 的类型注解。

    本模块用了 ``from __future__ import annotations``，注解是字符串，必须经
    get_type_hints 求值才能拿到真实类型。结果缓存，避免每条 journal 记录都
    重新求值一遍。
    """
    cached = _HINT_CACHE.get(tp)
    if cached is None:
        cached = get_type_hints(tp)
        _HINT_CACHE[tp] = cached
    return cached
