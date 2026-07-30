"""fsops 的例子级与边界测试。

正式属性（25 内容不变性、26 中断不丢文件、39 空目录唯一口径）在 M4/M5 实现。
本文件钉住的是这些原语自身的契约，特别是失败路径——因为「移动失败时文件去哪了」
决定了用户能不能把文件找回来。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core import fsops
from app.core.fsops import (
    MAX_PATH_LEN,
    atomic_move,
    copy_verify_trash,
    created_dir_chain,
    ensure_dir,
    is_effectively_empty,
    is_path_too_long,
    is_predicted_empty,
    next_available_name,
    same_volume,
    sanitize_parts,
    sanitize_segment,
    sha256_of,
)


# ---------------------------------------------------------------------------
# 命名清洗
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("财务", "财务"),
        ("财务/发票", "财务_发票"),
        (r"a\b", "a_b"),
        ("a:b", "a_b"),
        ("a*b?c", "a_b_c"),
        ('a"b', "a_b"),
        ("a<b>c|d", "a_b_c_d"),
    ],
)
def test_sanitize_replaces_illegal_chars(raw: str, expected: str) -> None:
    assert sanitize_segment(raw) == expected


def test_sanitize_strips_trailing_dots_and_spaces() -> None:
    """Windows 会静默丢弃结尾的点与空格，留着会让实际名字与预期不一致。"""
    assert sanitize_segment("报告. ") == "报告"
    assert sanitize_segment("报告...") == "报告"


def test_sanitize_never_returns_empty() -> None:
    assert sanitize_segment("...") == "_"
    assert sanitize_segment("") == "_"


@pytest.mark.parametrize("name", ["CON", "con", "PRN", "NUL", "COM1", "LPT9"])
def test_reserved_device_names_are_escaped(name: str) -> None:
    assert sanitize_segment(name) == "_" + name


def test_reserved_name_with_extension_is_also_escaped() -> None:
    """CON.txt 在 Windows 上同样不可用。"""
    assert sanitize_segment("CON.txt") == "_CON.txt"


def test_sanitize_parts_drops_empty_segments() -> None:
    assert sanitize_parts(("财务", "", "发票")) == ("财务", "发票")


def test_sanitize_parts_cleans_each_segment_independently() -> None:
    """段内的 "/" 是非法字符，段之间的层级不受影响。"""
    assert sanitize_parts(("财务/税务", "发票")) == ("财务_税务", "发票")


# ---------------------------------------------------------------------------
# 长路径
# ---------------------------------------------------------------------------


def test_path_length_boundary() -> None:
    assert not is_path_too_long("x" * MAX_PATH_LEN)
    assert is_path_too_long("x" * (MAX_PATH_LEN + 1))


# ---------------------------------------------------------------------------
# 自动重命名
# ---------------------------------------------------------------------------


def test_free_target_is_returned_unchanged(tmp_path: Path) -> None:
    target = tmp_path / "报告.pdf"
    assert next_available_name(target) == target


def test_existing_target_gets_numbered_suffix(tmp_path: Path) -> None:
    target = tmp_path / "报告.pdf"
    target.write_text("x", encoding="utf-8")

    assert next_available_name(target) == tmp_path / "报告 (2).pdf"


def test_suffix_increments_past_existing_numbers(tmp_path: Path) -> None:
    (tmp_path / "报告.pdf").write_text("x", encoding="utf-8")
    (tmp_path / "报告 (2).pdf").write_text("x", encoding="utf-8")

    assert next_available_name(tmp_path / "报告.pdf") == tmp_path / "报告 (3).pdf"


def test_already_numbered_name_continues_from_its_number(tmp_path: Path) -> None:
    (tmp_path / "报告 (2).pdf").write_text("x", encoding="utf-8")

    assert next_available_name(tmp_path / "报告 (2).pdf") == tmp_path / "报告 (3).pdf"


def test_taken_set_avoids_same_batch_collision(tmp_path: Path) -> None:
    """同一批次里两个文件算出同一目标时，只看磁盘会撞车。"""
    target = tmp_path / "报告.pdf"
    reserved = frozenset({target})

    assert next_available_name(target, taken=reserved) == tmp_path / "报告 (2).pdf"


def test_extension_is_preserved(tmp_path: Path) -> None:
    target = tmp_path / "存档.tar.gz"
    target.write_text("x", encoding="utf-8")

    assert next_available_name(target) == tmp_path / "存档.tar (2).gz"


# ---------------------------------------------------------------------------
# 空目录判定
# ---------------------------------------------------------------------------


def test_empty_directory_is_empty(tmp_path: Path) -> None:
    d = tmp_path / "empty"
    d.mkdir()
    assert is_effectively_empty(d)


def test_directory_with_file_is_not_empty(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    (d / "a.txt").write_text("x", encoding="utf-8")
    assert not is_effectively_empty(d)


def test_directory_with_only_subdir_is_not_empty(tmp_path: Path) -> None:
    d = tmp_path / "d"
    (d / "sub").mkdir(parents=True)
    assert not is_effectively_empty(d)


@pytest.mark.parametrize("junk", ["desktop.ini", "Thumbs.db"])
def test_directory_with_only_system_files_is_not_empty(tmp_path: Path, junk: str) -> None:
    """需求 20.12：仅含系统生成文件视为非空。单一口径天然满足，不需要白名单。"""
    d = tmp_path / "d"
    d.mkdir()
    (d / junk).write_text("x", encoding="utf-8")
    assert not is_effectively_empty(d)


def test_missing_directory_is_treated_as_not_empty(tmp_path: Path) -> None:
    """读不到内容时宁可留下目录，也不能误删。"""
    assert not is_effectively_empty(tmp_path / "不存在")


def test_predicted_empty_subtracts_moved_out() -> None:
    listing = frozenset({Path("a.pdf"), Path("b.pdf")})

    assert is_predicted_empty(listing, frozenset({Path("a.pdf"), Path("b.pdf")}))
    assert not is_predicted_empty(listing, frozenset({Path("a.pdf")}))
    assert not is_predicted_empty(listing, frozenset())


# ---------------------------------------------------------------------------
# 目录创建
# ---------------------------------------------------------------------------


def test_ensure_dir_reports_creation(tmp_path: Path) -> None:
    d = tmp_path / "文档" / "PDF"

    assert ensure_dir(d) is True
    assert d.is_dir()
    assert ensure_dir(d) is False


def test_created_dir_chain_lists_shallow_to_deep(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    deep = root / "文档" / "PDF" / "2024"

    chain = created_dir_chain(deep, stop_at=root)

    assert chain == (root / "文档", root / "文档" / "PDF", deep)


def test_created_dir_chain_is_empty_when_dir_exists(tmp_path: Path) -> None:
    root = tmp_path / "root"
    existing = root / "文档"
    existing.mkdir(parents=True)

    assert created_dir_chain(existing, stop_at=root) == ()


# ---------------------------------------------------------------------------
# 卷与哈希
# ---------------------------------------------------------------------------


def test_same_volume_for_two_paths_in_tmp(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert same_volume(a, b)


def test_same_volume_works_for_not_yet_existing_target(tmp_path: Path) -> None:
    """目标目录通常还没建出来，判定要落到最近的已存在祖先上。"""
    src = tmp_path / "a.pdf"
    src.write_text("x", encoding="utf-8")
    future = tmp_path / "文档" / "PDF" / "a.pdf"

    assert same_volume(src, future)


def test_sha256_matches_hashlib(tmp_path: Path) -> None:
    import hashlib

    payload = b"\x00\x01\x02" * 5000
    f = tmp_path / "blob.bin"
    f.write_bytes(payload)

    assert sha256_of(f) == hashlib.sha256(payload).hexdigest()


def test_sha256_of_empty_file(tmp_path: Path) -> None:
    import hashlib

    f = tmp_path / "empty.bin"
    f.write_bytes(b"")

    assert sha256_of(f) == hashlib.sha256(b"").hexdigest()


# ---------------------------------------------------------------------------
# atomic_move
# ---------------------------------------------------------------------------


def test_atomic_move_relocates_file(tmp_path: Path) -> None:
    src = tmp_path / "a.pdf"
    src.write_text("内容", encoding="utf-8")
    dst_dir = tmp_path / "文档"
    dst_dir.mkdir()
    dst = dst_dir / "a.pdf"

    atomic_move(src, dst)

    assert not src.exists()
    assert dst.read_text(encoding="utf-8") == "内容"


# ---------------------------------------------------------------------------
# copy_verify_trash
# ---------------------------------------------------------------------------


class TrashRecorder:
    """替换 send2trash，记录被移入回收站的路径（属性 27 依赖它）。"""

    def __init__(self) -> None:
        self.trashed: list[Path] = []

    def __call__(self, path: Path) -> None:
        self.trashed.append(path)
        os.unlink(path)


def test_cross_volume_move_copies_verifies_and_trashes_source(tmp_path: Path) -> None:
    src = tmp_path / "a.pdf"
    src.write_bytes(b"payload" * 1000)
    dst = tmp_path / "out" / "a.pdf"
    dst.parent.mkdir()
    recorder = TrashRecorder()

    outcome = copy_verify_trash(src, dst, trash=recorder)

    assert outcome.ok
    assert outcome.trashed_source
    assert outcome.size == len(b"payload" * 1000)
    assert outcome.sha256 == sha256_of(dst)
    assert recorder.trashed == [src]
    assert not src.exists()


def test_cross_volume_keeps_source_when_verification_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """需求 12.3：校验不一致时删目标、保源、标记失败。

    这是「移动过程物理上不可能丢数据」这条承诺最关键的一条分支。
    """
    src = tmp_path / "a.pdf"
    src.write_bytes(b"payload")
    dst = tmp_path / "out" / "a.pdf"
    dst.parent.mkdir()
    recorder = TrashRecorder()

    calls = {"n": 0}
    real_sha = fsops.sha256_of

    def flaky_sha(path: Path, chunk: int = 1 << 20) -> str:
        calls["n"] += 1
        if calls["n"] == 2:  # 目标那次返回不同的哈希
            return "0" * 64
        return real_sha(path, chunk)

    monkeypatch.setattr(fsops, "sha256_of", flaky_sha)

    outcome = copy_verify_trash(src, dst, trash=recorder)

    assert not outcome.ok
    assert outcome.error is not None
    assert src.exists(), "校验失败必须保留源文件"
    assert not dst.exists(), "不完整的目标副本必须被清掉"
    assert recorder.trashed == [], "校验失败时不应动回收站"


def test_cross_volume_reports_failure_when_source_unreadable(tmp_path: Path) -> None:
    missing = tmp_path / "不存在.pdf"
    dst = tmp_path / "out" / "a.pdf"
    dst.parent.mkdir()

    outcome = copy_verify_trash(missing, dst, trash=TrashRecorder())

    assert not outcome.ok
    assert not dst.exists()


def test_cross_volume_keeps_both_copies_when_trash_fails(tmp_path: Path) -> None:
    """目标已校验通过但源没能进回收站：两份都留着，让用户处置。

    删哪一份都有丢数据的风险，所以刻意什么都不删。
    """
    src = tmp_path / "a.pdf"
    src.write_bytes(b"payload")
    dst = tmp_path / "out" / "a.pdf"
    dst.parent.mkdir()

    def failing_trash(path: Path) -> None:
        raise OSError("回收站不可用")

    outcome = copy_verify_trash(src, dst, trash=failing_trash)

    assert not outcome.ok
    assert src.exists()
    assert dst.exists()


def test_verify_hash_can_be_disabled(tmp_path: Path) -> None:
    src = tmp_path / "a.bin"
    src.write_bytes(b"x" * 10)
    dst = tmp_path / "out" / "a.bin"
    dst.parent.mkdir()

    outcome = copy_verify_trash(src, dst, verify_hash=False, trash=TrashRecorder())

    assert outcome.ok
    assert outcome.sha256 is None
