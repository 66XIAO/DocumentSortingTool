"""Settings dataclass 树与 SettingsManager。

本层从 ``Settings`` 构造 core 的窄选项对象，因此 config -> core 是单向依赖，不成环
（需求 18.7）。

## 三条实现约束

**永不抛异常地读配置。** ``load()`` 对任何形式的损坏（YAML 语法错误、字段类型不对、
缺字段、多出未知键）都返回一个合法的 ``Settings`` 并把文件重写成合法内容
（需求 18.2、属性 10）。配置读不出来不该让工具打不开。

**api_key 从不落文件。** 文件里只写 ``api_key_ref: "keyring:DocSorter/<provider_id>"``，
真值在 Windows 凭据管理器（需求 7.2、7.3）。因此配置往返（需求 18.3、属性 30）的
等价性比较范围天然排除明文密钥——它压根不在文件里。

**取值范围校验落在这里。** 需求给出的上下界（`min_confidence` 在 0..1、taxonomy
类目数 ≤12、层级 ≤2、批大小 80..120、抽样 ≤300）在加载时就被夹紧，而不是等到
调用 LLM 时才发现配置越界。

## scan.scope 为什么同时存在于 Settings 和 ScanSelection

``ScanSettings.scope`` 是**持久化**的值，用于下次打开时恢复界面状态（需求 18.8）；
运行期的真实范围由 ``ScanSelection.scope()`` 派生（需求 2.9）。``ScanOptions``
刻意不含该字段，避免出现两个事实来源。
"""

from __future__ import annotations

import copy
import dataclasses
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.core.models import (
    ActionKind,
    AIOptions,
    ClassifyOptions,
    ConflictPolicy,
    DateGranularity,
    ExecOptions,
    PrivacyLevel,
    ProviderKind,
    ScanOptions,
    ScanScope,
    Strategy,
    from_jsonable,
    to_jsonable,
)

logger = logging.getLogger(__name__)

APP_NAME = "DocSorter"
KEYRING_SERVICE = "DocSorter"
SETTINGS_VERSION = 1


# ---------------------------------------------------------------------------
# Settings 树
# ---------------------------------------------------------------------------


@dataclass
class ScanSettings:
    scope: ScanScope = ScanScope.TOP_LEVEL_ONLY  # 需求 2.1、18.8
    include_hidden: bool = False  # 需求 2.17
    follow_symlinks: bool = False  # 需求 1.6


@dataclass
class ClassifySettings:
    strategy: Strategy = Strategy.BY_TYPE  # 需求 3.2
    min_confidence: float = 0.6  # 需求 3.7
    merge_small_categories: bool = False  # 需求 3.12
    small_category_threshold: int = 3


@dataclass
class DateSettings:
    granularity: DateGranularity = DateGranularity.YEAR_MONTH  # 需求 3.4


@dataclass
class ConflictSettings:
    policy: ConflictPolicy = ConflictPolicy.AUTO_RENAME  # 需求 9.4
    default_action: ActionKind = ActionKind.MOVE  # 需求 11.1
    verify_hash: bool = True  # 需求 12.2


@dataclass
class CleanupSettings:
    remove_empty_dirs: bool = False  # 需求 20.1、18.8


@dataclass
class AISettings:
    enabled: bool = False  # 需求 6.1
    provider: ProviderKind = ProviderKind.OPENAI_COMPAT  # 需求 7.1
    base_url: str = ""
    host: str = "http://localhost:11434"
    model: str = ""
    timeout_seconds: int = 30  # 需求 6.8
    privacy_level: PrivacyLevel = PrivacyLevel.METADATA_ONLY  # 需求 8.1
    #: 只存引用，不存明文（需求 7.3）
    api_key_ref: str = ""
    taxonomy_sample_size: int = 300  # 需求 7.5 上界
    max_categories: int = 12  # 需求 7.6 上界
    max_depth: int = 2  # 需求 7.6 上界
    batch_size: int = 100  # 需求 7.7 的 80..120


@dataclass
class HistorySettings:
    max_runs: int = 20  # 需求 15.3
    max_days: int = 30


@dataclass
class UISettings:
    recent_roots: tuple[str, ...] = ()
    #: 已完成过一次模拟运行的根目录。需求 11.2 要求首次对某目录执行前强制先
    #: 模拟运行一次，「是否首次」必须跨会话记住，否则重启即绕过这道防护。
    dry_run_completed_roots: tuple[str, ...] = ()


@dataclass
class Settings:
    version: int = SETTINGS_VERSION
    scan: ScanSettings = field(default_factory=ScanSettings)
    classify: ClassifySettings = field(default_factory=ClassifySettings)
    date: DateSettings = field(default_factory=DateSettings)
    conflict: ConflictSettings = field(default_factory=ConflictSettings)
    cleanup: CleanupSettings = field(default_factory=CleanupSettings)
    ai: AISettings = field(default_factory=AISettings)
    history: HistorySettings = field(default_factory=HistorySettings)
    ui: UISettings = field(default_factory=UISettings)


# ---------------------------------------------------------------------------
# 取值范围
# ---------------------------------------------------------------------------

#: 字段路径 -> (最小值, 最大值)。上下界直接来自需求，加载时夹紧。
_BOUNDS: dict[tuple[str, str], tuple[float, float]] = {
    ("classify", "min_confidence"): (0.0, 1.0),
    ("classify", "small_category_threshold"): (1, 1000),
    ("ai", "timeout_seconds"): (1, 600),
    ("ai", "taxonomy_sample_size"): (1, 300),
    ("ai", "max_categories"): (1, 12),
    ("ai", "max_depth"): (1, 2),
    ("ai", "batch_size"): (80, 120),
    ("history", "max_runs"): (1, 1000),
    ("history", "max_days"): (1, 3650),
}


def default_app_dir() -> Path:
    """配置与历史记录的根目录。

    设计里写的是 ``Path(os.environ["APPDATA"])`` 直接作为类属性求值。这里改成
    函数并加回落：``APPDATA`` 缺失时（非 Windows、精简容器）直接 KeyError 会让
    模块在 import 期就崩，连报错界面都弹不出来。
    """
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / APP_NAME
    return Path.home() / f".{APP_NAME.lower()}"


def packaged_rules_path() -> Path:
    """随包分发的内置规则文件位置。"""
    return Path(__file__).resolve().parent / "rules_default.yaml"


# ---------------------------------------------------------------------------
# SettingsManager
# ---------------------------------------------------------------------------


class SettingsManager:
    """配置读写与凭据存取。

    ``base_dir`` 与 ``keyring_backend`` 都可注入：测试不该往真实的
    ``%APPDATA%`` 写文件，也不该依赖机器上存在可用的凭据管理器。
    """

    SETTINGS_FILENAME = "settings.yaml"
    RULES_FILENAME = "rules.yaml"

    def __init__(
        self,
        base_dir: Path | None = None,
        keyring_backend: Any | None = None,
    ) -> None:
        self._base_dir = Path(base_dir) if base_dir else default_app_dir()
        self._keyring = keyring_backend
        self._warnings: list[str] = []

    # -- 路径 -------------------------------------------------------------

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def settings_path(self) -> Path:
        return self._base_dir / self.SETTINGS_FILENAME

    @property
    def rules_path(self) -> Path:
        return self._base_dir / self.RULES_FILENAME

    @property
    def history_dir(self) -> Path:
        return self._base_dir / "history"

    @property
    def warnings(self) -> list[str]:
        """上一次 ``load`` 记录的问题，供设置页展示。"""
        return list(self._warnings)

    # -- 首次运行 ---------------------------------------------------------

    def ensure_user_files(self) -> None:
        """首次运行释放规则文件并落地默认配置。需求 4.1。"""
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self.history_dir.mkdir(parents=True, exist_ok=True)

        if not self.rules_path.exists():
            source = packaged_rules_path()
            if source.exists():
                shutil.copyfile(source, self.rules_path)
            else:
                # rules_default.yaml 由任务 14 落地。缺失时不阻断启动——
                # RuleEngine 会回落到内置默认规则（需求 4.3）。
                logger.warning("内置规则文件缺失: %s", source)

        if not self.settings_path.exists():
            self.save(Settings())

    # -- 读写 -------------------------------------------------------------

    def load(self) -> Settings:
        """读配置。任何形式的损坏都返回合法对象并重写文件。需求 18.2。"""
        self._warnings = []

        if not self.settings_path.exists():
            settings = Settings()
            self.save(settings)
            return settings

        raw: Any
        try:
            text = self.settings_path.read_text(encoding="utf-8")
            raw = yaml.safe_load(text)
        except (OSError, yaml.YAMLError) as exc:
            self._warn(f"配置文件无法解析，已重置为默认值: {exc}")
            settings = Settings()
            self.save(settings)
            return settings

        if not isinstance(raw, dict):
            self._warn("配置文件的顶层结构不是映射，已重置为默认值")
            settings = Settings()
            self.save(settings)
            return settings

        settings = self._coerce(Settings, raw, path=())
        settings = self._apply_bounds(settings)

        if self._warnings:
            # 需求 18.2 要求「使用默认值填充并重写该文件」
            self.save(settings)
        return settings

    def save(self, settings: Settings) -> None:
        """写配置。

        用共用的 ``to_jsonable`` 而非设计里写的 ``asdict``：``asdict`` 会把枚举和
        ``Path`` 原样留下，``yaml.safe_dump`` 无法序列化它们；而 ``to_jsonable``
        是六处往返共用的那一套转换规则，配置往返因此和 journal / manifest 走同一
        条代码路径。
        """
        self._base_dir.mkdir(parents=True, exist_ok=True)
        payload = to_jsonable(settings)
        text = yaml.safe_dump(
            payload, sort_keys=False, allow_unicode=True, default_flow_style=False
        )
        tmp = self.settings_path.with_suffix(".yaml.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.settings_path)

    # -- 凭据 -------------------------------------------------------------

    def api_key_ref(self, provider_id: str) -> str:
        """文件里存的引用形式。需求 7.3。"""
        return f"keyring:{KEYRING_SERVICE}/{provider_id}"

    def get_api_key(self, provider_id: str) -> str | None:
        backend = self._keyring_backend()
        if backend is None:
            return None
        try:
            return backend.get_password(KEYRING_SERVICE, provider_id)
        except Exception as exc:  # noqa: BLE001 - 凭据后端不可用不该让工具崩
            self._warn(f"读取凭据失败: {exc}")
            return None

    def set_api_key(self, provider_id: str, key: str) -> bool:
        backend = self._keyring_backend()
        if backend is None:
            return False
        try:
            backend.set_password(KEYRING_SERVICE, provider_id, key)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"写入凭据失败: {exc}")
            return False
        return True

    def delete_api_key(self, provider_id: str) -> bool:
        backend = self._keyring_backend()
        if backend is None:
            return False
        try:
            backend.delete_password(KEYRING_SERVICE, provider_id)
        except Exception:  # noqa: BLE001 - 不存在也算删除成功
            return False
        return True

    def _keyring_backend(self) -> Any | None:
        if self._keyring is not None:
            return self._keyring
        try:
            import keyring
        except ImportError:  # pragma: no cover - 依赖已锁定，仅防御
            self._warn("未安装 keyring，无法保存 API 密钥")
            return None
        return keyring

    # -- 转换为 core 的窄选项对象 -----------------------------------------

    @staticmethod
    def to_scan_options(settings: Settings) -> ScanOptions:
        """注意不传 scope：运行期范围由 ScanSelection 派生，避免两个事实来源。"""
        return ScanOptions(
            include_hidden=settings.scan.include_hidden,
            follow_symlinks=settings.scan.follow_symlinks,
        )

    @staticmethod
    def to_classify_options(settings: Settings) -> ClassifyOptions:
        return ClassifyOptions(
            strategy=settings.classify.strategy,
            min_confidence=settings.classify.min_confidence,
            date_granularity=settings.date.granularity,
            merge_small_categories=settings.classify.merge_small_categories,
            small_category_threshold=settings.classify.small_category_threshold,
        )

    @staticmethod
    def to_exec_options(settings: Settings, dry_run: bool = False) -> ExecOptions:
        return ExecOptions(
            dry_run=dry_run,
            conflict_policy=settings.conflict.policy,
            remove_empty_dirs=settings.cleanup.remove_empty_dirs,
            verify_hash=settings.conflict.verify_hash,
            default_action=settings.conflict.default_action,
        )

    @staticmethod
    def to_ai_options(settings: Settings) -> AIOptions:
        return AIOptions(
            enabled=settings.ai.enabled,
            provider=settings.ai.provider,
            base_url=settings.ai.base_url,
            host=settings.ai.host,
            model=settings.ai.model,
            timeout_seconds=settings.ai.timeout_seconds,
            privacy_level=settings.ai.privacy_level,
            taxonomy_sample_size=settings.ai.taxonomy_sample_size,
            max_categories=settings.ai.max_categories,
            max_depth=settings.ai.max_depth,
            batch_size=settings.ai.batch_size,
        )

    # -- 容错解码 ---------------------------------------------------------

    def _warn(self, message: str) -> None:
        self._warnings.append(message)
        logger.warning("%s", message)

    def _coerce(self, cls: type, raw: Any, path: tuple[str, ...]) -> Any:
        """逐字段容错解码：未知键丢弃、非法值回落默认并记 warning。

        happy path 复用 ``from_jsonable``（与 journal / manifest 同一套规则），
        失败时才回落到字段默认值。这样「合法配置的往返」与「损坏配置的容错」共用
        同一份类型知识，不会出现两边理解不一致。
        """
        if not isinstance(raw, dict):
            self._warn(f"{self._label(path)} 不是映射，已使用默认值")
            return cls()

        hints = {f.name: f for f in dataclasses.fields(cls)}
        for key in raw:
            if key not in hints:
                self._warn(f"{self._label(path)} 含未知字段 {key!r}，已忽略")

        kwargs: dict[str, Any] = {}
        for name, f in hints.items():
            if name not in raw:
                continue
            value = raw[name]
            field_type = f.type
            resolved = _resolve(field_type)

            if dataclasses.is_dataclass(resolved) and isinstance(resolved, type):
                kwargs[name] = self._coerce(resolved, value, (*path, name))
                continue

            try:
                kwargs[name] = from_jsonable(value, resolved)
            except (TypeError, ValueError, KeyError, AttributeError):
                self._warn(
                    f"{self._label((*path, name))} 的取值 {value!r} 非法，已使用默认值"
                )

        try:
            return cls(**kwargs)
        except (TypeError, ValueError) as exc:
            self._warn(f"{self._label(path)} 构造失败（{exc}），已使用默认值")
            return cls()

    def _apply_bounds(self, settings: Settings) -> Settings:
        """把越界取值夹回需求给出的范围。"""
        result = copy.deepcopy(settings)
        for (group, name), (low, high) in _BOUNDS.items():
            section = getattr(result, group)
            value = getattr(section, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            clamped = min(max(value, low), high)
            if clamped != value:
                self._warn(
                    f"{group}.{name} 的取值 {value} 超出范围 [{low}, {high}]，已夹紧为 {clamped}"
                )
                setattr(section, name, type(value)(clamped))
        if result.version != SETTINGS_VERSION:
            self._warn(
                f"配置版本 {result.version} 与当前版本 {SETTINGS_VERSION} 不一致"
            )
            result.version = SETTINGS_VERSION
        return result

    @staticmethod
    def _label(path: tuple[str, ...]) -> str:
        return ".".join(path) if path else "配置根"


_TYPE_CACHE: dict[str, Any] = {}


def _resolve(annotation: Any) -> Any:
    """把字符串注解求值成真实类型。

    本模块用了 ``from __future__ import annotations``，dataclass 字段的 ``type``
    是字符串。
    """
    if not isinstance(annotation, str):
        return annotation
    cached = _TYPE_CACHE.get(annotation)
    if cached is not None:
        return cached
    try:
        resolved = eval(annotation, globals())  # noqa: S307 - 仅求值本模块自己的注解
    except Exception:  # noqa: BLE001
        resolved = Any
    _TYPE_CACHE[annotation] = resolved
    return resolved
