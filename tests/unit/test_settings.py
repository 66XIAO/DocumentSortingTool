"""SettingsManager 的例子级与边界测试。

属性 30（配置往返）与属性 10 的配置侧在任务 12 用 Hypothesis 实现。本文件重点在
容错分支——配置读不出来不该让工具打不开，这是需求 18.2 的实质。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.config.settings import (
    KEYRING_SERVICE,
    SETTINGS_VERSION,
    Settings,
    SettingsManager,
    default_app_dir,
)
from app.core.models import (
    ActionKind,
    ConflictPolicy,
    DateGranularity,
    PrivacyLevel,
    ProviderKind,
    ScanScope,
    Strategy,
)


class MemoryKeyring:
    """替换 keyring 后端，使凭据测试不依赖真实凭据管理器。"""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.fail = False

    def get_password(self, service: str, user: str) -> str | None:
        if self.fail:
            raise RuntimeError("凭据后端不可用")
        return self.store.get((service, user))

    def set_password(self, service: str, user: str, password: str) -> None:
        if self.fail:
            raise RuntimeError("凭据后端不可用")
        self.store[(service, user)] = password

    def delete_password(self, service: str, user: str) -> None:
        del self.store[(service, user)]


@pytest.fixture
def manager(tmp_path: Path) -> SettingsManager:
    return SettingsManager(base_dir=tmp_path / "DocSorter", keyring_backend=MemoryKeyring())


def _write(manager: SettingsManager, payload: object) -> None:
    manager.base_dir.mkdir(parents=True, exist_ok=True)
    manager.settings_path.write_text(
        yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 默认值
# ---------------------------------------------------------------------------


def test_defaults_match_requirements() -> None:
    s = Settings()

    assert s.scan.scope is ScanScope.TOP_LEVEL_ONLY  # 需求 2.1
    assert s.scan.include_hidden is False  # 需求 2.17
    assert s.classify.strategy is Strategy.BY_TYPE  # 需求 3.2
    assert s.classify.min_confidence == 0.6  # 需求 3.7
    assert s.classify.small_category_threshold == 3  # 需求 3.12
    assert s.date.granularity is DateGranularity.YEAR_MONTH
    assert s.conflict.policy is ConflictPolicy.AUTO_RENAME  # 需求 9.4
    assert s.conflict.default_action is ActionKind.MOVE  # 需求 11.1
    assert s.cleanup.remove_empty_dirs is False  # 需求 20.1
    assert s.ai.enabled is False  # 需求 6.1
    assert s.ai.timeout_seconds == 30  # 需求 6.8
    assert s.ai.privacy_level is PrivacyLevel.METADATA_ONLY  # 需求 8.1
    assert s.history.max_runs == 20  # 需求 15.3
    assert s.history.max_days == 30


def test_default_app_dir_prefers_appdata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", r"D:\Roaming")
    assert default_app_dir() == Path(r"D:\Roaming\DocSorter")


def test_default_app_dir_falls_back_without_appdata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """APPDATA 缺失时不能在 import 期崩掉，否则连报错界面都弹不出来。"""
    monkeypatch.delenv("APPDATA", raising=False)
    assert default_app_dir() == Path.home() / ".docsorter"


# ---------------------------------------------------------------------------
# 首次运行
# ---------------------------------------------------------------------------


def test_ensure_user_files_creates_dirs_and_settings(manager: SettingsManager) -> None:
    manager.ensure_user_files()

    assert manager.base_dir.is_dir()
    assert manager.history_dir.is_dir()
    assert manager.settings_path.exists()


def test_ensure_user_files_is_idempotent(manager: SettingsManager) -> None:
    manager.ensure_user_files()
    settings = manager.load()
    settings.ai.model = "自定义"
    manager.save(settings)

    manager.ensure_user_files()

    assert manager.load().ai.model == "自定义"


def test_ensure_user_files_tolerates_missing_packaged_rules(
    manager: SettingsManager,
) -> None:
    """rules_default.yaml 由任务 14 落地，此刻缺失不该阻断启动。"""
    manager.ensure_user_files()

    assert manager.settings_path.exists()


# ---------------------------------------------------------------------------
# 往返
# ---------------------------------------------------------------------------


def test_round_trip_preserves_every_field(manager: SettingsManager) -> None:
    original = Settings()
    original.scan.scope = ScanScope.SELECTED_SUBFOLDERS
    original.scan.include_hidden = True
    original.scan.follow_symlinks = True
    original.classify.strategy = Strategy.SMART
    original.classify.min_confidence = 0.75
    original.classify.merge_small_categories = True
    original.classify.small_category_threshold = 5
    original.date.granularity = DateGranularity.YEAR
    original.conflict.policy = ConflictPolicy.SKIP
    original.conflict.default_action = ActionKind.COPY
    original.conflict.verify_hash = False
    original.cleanup.remove_empty_dirs = True
    original.ai.enabled = True
    original.ai.provider = ProviderKind.OLLAMA
    original.ai.model = "qwen2.5:7b"
    original.ai.privacy_level = PrivacyLevel.METADATA_PLUS_HEAD500
    original.ai.timeout_seconds = 45
    original.history.max_runs = 50
    original.history.max_days = 90
    original.ui.recent_roots = ("D:\\下载", "E:\\资料")
    original.ui.dry_run_completed_roots = ("D:\\下载",)

    manager.save(original)
    restored = manager.load()

    assert restored == original
    assert manager.warnings == []


def test_round_trip_preserves_the_two_required_fields(manager: SettingsManager) -> None:
    """需求 18.8 点名了这两个字段。"""
    original = Settings()
    original.scan.scope = ScanScope.SELECTED_SUBFOLDERS
    original.cleanup.remove_empty_dirs = True

    manager.save(original)
    restored = manager.load()

    assert restored.scan.scope is ScanScope.SELECTED_SUBFOLDERS
    assert restored.cleanup.remove_empty_dirs is True


def test_enums_are_written_as_plain_strings(manager: SettingsManager) -> None:
    """YAML 里应当是可读的字符串，而不是 Python 对象标签。"""
    manager.save(Settings())

    text = manager.settings_path.read_text(encoding="utf-8")

    assert "top_level_only" in text
    assert "!!python" not in text


def test_yaml_allows_unicode(manager: SettingsManager) -> None:
    settings = Settings()
    settings.ui.recent_roots = ("D:\\下载",)
    manager.save(settings)

    assert "下载" in manager.settings_path.read_text(encoding="utf-8")


def test_save_is_atomic_and_leaves_no_temp_file(manager: SettingsManager) -> None:
    manager.save(Settings())

    leftovers = list(manager.base_dir.glob("*.tmp"))
    assert leftovers == []


# ---------------------------------------------------------------------------
# 容错（需求 18.2、属性 10）
# ---------------------------------------------------------------------------


def test_missing_file_yields_defaults_and_writes_it(manager: SettingsManager) -> None:
    assert not manager.settings_path.exists()

    settings = manager.load()

    assert settings == Settings()
    assert manager.settings_path.exists()


def test_broken_yaml_is_reset(manager: SettingsManager) -> None:
    manager.base_dir.mkdir(parents=True, exist_ok=True)
    manager.settings_path.write_text("scan:\n  scope: [unclosed\n", encoding="utf-8")

    settings = manager.load()

    assert settings == Settings()
    assert manager.warnings
    # 重写后应当能再读出来且不再报警
    manager.load()


def test_non_mapping_root_is_reset(manager: SettingsManager) -> None:
    _write(manager, ["a", "b"])

    settings = manager.load()

    assert settings == Settings()
    assert manager.warnings


def test_unknown_fields_are_dropped_with_warning(manager: SettingsManager) -> None:
    _write(manager, {"scan": {"scope": "top_level_only", "未知": 1}, "多余": True})

    settings = manager.load()

    assert settings.scan.scope is ScanScope.TOP_LEVEL_ONLY
    assert any("未知" in w for w in manager.warnings)
    assert any("多余" in w for w in manager.warnings)


def test_wrong_enum_value_falls_back_to_default(manager: SettingsManager) -> None:
    _write(manager, {"classify": {"strategy": "不存在的策略"}})

    settings = manager.load()

    assert settings.classify.strategy is Strategy.BY_TYPE
    assert manager.warnings


def test_wrong_type_falls_back_to_default(manager: SettingsManager) -> None:
    _write(manager, {"scan": {"include_hidden": "是的"}, "history": {"max_runs": "很多"}})

    settings = manager.load()

    assert settings.history.max_runs == 20
    assert manager.warnings


def test_missing_field_uses_default(manager: SettingsManager) -> None:
    _write(manager, {"scan": {"include_hidden": True}})

    settings = manager.load()

    assert settings.scan.include_hidden is True
    assert settings.scan.follow_symlinks is False
    assert settings.classify.strategy is Strategy.BY_TYPE


def test_section_that_is_not_a_mapping_falls_back(manager: SettingsManager) -> None:
    _write(manager, {"ai": "开"})

    settings = manager.load()

    assert settings.ai.enabled is False
    assert manager.warnings


def test_corrupted_file_is_rewritten_as_valid(manager: SettingsManager) -> None:
    """需求 18.2：非法字段要「使用默认值填充并重写该文件」。"""
    _write(manager, {"classify": {"strategy": "乱码"}})

    manager.load()
    reread = SettingsManager(
        base_dir=manager.base_dir, keyring_backend=MemoryKeyring()
    ).load()

    assert reread.classify.strategy is Strategy.BY_TYPE
    assert reread == Settings()


def test_load_never_raises_on_arbitrary_junk(manager: SettingsManager) -> None:
    for payload in (
        {"version": "x"},
        {"scan": {"scope": 123}},
        {"ai": {"timeout_seconds": None}},
        {"ui": {"recent_roots": "不是列表"}},
        {"history": {"max_days": [1, 2]}},
        {"classify": {"min_confidence": "高"}},
    ):
        _write(manager, payload)
        assert isinstance(manager.load(), Settings)


# ---------------------------------------------------------------------------
# 取值范围
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("group", "name", "given", "expected"),
    [
        ("classify", "min_confidence", 1.8, 1.0),
        ("classify", "min_confidence", -0.5, 0.0),
        ("classify", "small_category_threshold", 0, 1),
        ("ai", "timeout_seconds", 0, 1),
        ("ai", "max_categories", 99, 12),  # 需求 7.6
        ("ai", "max_depth", 5, 2),  # 需求 7.6
        ("ai", "batch_size", 500, 120),  # 需求 7.7
        ("ai", "batch_size", 3, 80),
        ("ai", "taxonomy_sample_size", 9999, 300),  # 需求 7.5
        ("history", "max_runs", 0, 1),
        ("history", "max_days", 0, 1),
    ],
)
def test_out_of_range_values_are_clamped(
    manager: SettingsManager, group: str, name: str, given: object, expected: object
) -> None:
    _write(manager, {group: {name: given}})

    settings = manager.load()

    assert getattr(getattr(settings, group), name) == expected
    assert manager.warnings


def test_version_mismatch_is_normalised(manager: SettingsManager) -> None:
    _write(manager, {"version": 99})

    settings = manager.load()

    assert settings.version == SETTINGS_VERSION
    assert manager.warnings


# ---------------------------------------------------------------------------
# 凭据
# ---------------------------------------------------------------------------


def test_api_key_ref_format(manager: SettingsManager) -> None:
    assert manager.api_key_ref("openai_compat") == f"keyring:{KEYRING_SERVICE}/openai_compat"


def test_api_key_round_trips_through_backend(manager: SettingsManager) -> None:
    assert manager.set_api_key("openai_compat", "sk-secret") is True
    assert manager.get_api_key("openai_compat") == "sk-secret"


def test_api_key_never_reaches_the_settings_file(manager: SettingsManager) -> None:
    """需求 7.3：文件里只有引用键，没有明文。"""
    manager.set_api_key("openai_compat", "sk-should-not-appear")
    settings = Settings()
    settings.ai.api_key_ref = manager.api_key_ref("openai_compat")
    manager.save(settings)

    text = manager.settings_path.read_text(encoding="utf-8")

    assert "sk-should-not-appear" not in text
    assert "keyring:DocSorter/openai_compat" in text


def test_unknown_api_key_is_none(manager: SettingsManager) -> None:
    assert manager.get_api_key("never_set") is None


def test_keyring_failure_is_reported_not_raised(tmp_path: Path) -> None:
    backend = MemoryKeyring()
    backend.fail = True
    manager = SettingsManager(base_dir=tmp_path / "d", keyring_backend=backend)

    assert manager.set_api_key("p", "k") is False
    assert manager.get_api_key("p") is None
    assert manager.warnings


# ---------------------------------------------------------------------------
# 转换为 core 的窄选项对象
# ---------------------------------------------------------------------------


def test_scan_options_omit_scope() -> None:
    """运行期范围由 ScanSelection 派生，选项对象不该带第二个事实来源。"""
    settings = Settings()
    settings.scan.scope = ScanScope.SELECTED_SUBFOLDERS
    settings.scan.include_hidden = True

    options = SettingsManager.to_scan_options(settings)

    assert not hasattr(options, "scope")
    assert options.include_hidden is True
    assert options.excluded_names == frozenset({".docsort"})


def test_classify_options_pull_granularity_from_date_section() -> None:
    settings = Settings()
    settings.date.granularity = DateGranularity.YEAR
    settings.classify.min_confidence = 0.8

    options = SettingsManager.to_classify_options(settings)

    assert options.date_granularity is DateGranularity.YEAR
    assert options.min_confidence == 0.8


def test_exec_options_carry_dry_run_flag() -> None:
    settings = Settings()
    settings.cleanup.remove_empty_dirs = True
    settings.conflict.policy = ConflictPolicy.OVERWRITE

    assert SettingsManager.to_exec_options(settings).dry_run is False
    dry = SettingsManager.to_exec_options(settings, dry_run=True)

    assert dry.dry_run is True
    assert dry.remove_empty_dirs is True
    assert dry.conflict_policy is ConflictPolicy.OVERWRITE
    assert dry.default_action is ActionKind.MOVE


def test_ai_options_mirror_ai_section() -> None:
    settings = Settings()
    settings.ai.enabled = True
    settings.ai.provider = ProviderKind.OLLAMA
    settings.ai.model = "qwen2.5:7b"
    settings.ai.privacy_level = PrivacyLevel.METADATA_PLUS_HEAD500

    options = SettingsManager.to_ai_options(settings)

    assert options.enabled is True
    assert options.provider is ProviderKind.OLLAMA
    assert options.model == "qwen2.5:7b"
    assert options.privacy_level is PrivacyLevel.METADATA_PLUS_HEAD500
