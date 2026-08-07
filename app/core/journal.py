"""Journal：操作日志与现场快照。需求 13。

这个模块承载「误操作必须可完整复原」这条红线的可追溯性部分。三条实现约束：

**主副本是唯一权威。** `%APPDATA%\\DocSorter\\history\\<run_id>\\` 下的
`manifest.json` 与 `journal.jsonl` 是撤销所依据的唯一来源（需求 13.6）。根目录下的
`.docsort\\<run_id>\\` 只是镜像，方便用户自查、也让文件夹搬到别的机器后仍有记录
（需求 13.5）。镜像写失败**只记 warning 不中断**——根目录可能只读或空间不足，
但那不该让整次整理失败。

**每条记录写完就 fsync。** `flush()` 只把数据交给操作系统，掉电仍会丢。撤销的
正确性依赖「日志里写了的就是真做了的」，所以必须 `os.fsync`（需求 13.3、13.4）。
文件句柄在整个 run 期间保持打开，避免每条重新 open 的开销。

**记录文本表示唯一。** `sort_keys=True` 让同一条记录序列化出的字节完全确定，
往返比较（需求 13.9）与镜像一致性校验才有意义。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import TextIO

from app.core.models import (
    JournalRecord,
    Manifest,
    RecordKind,
    from_jsonable,
    to_jsonable,
)

logger = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"
JOURNAL_NAME = "journal.jsonl"
MIRROR_DIR_NAME = ".docsort"


def new_run_id(clock: object | None = None) -> str:
    """时间戳 + 随机后缀。

    加随机后缀是因为同一秒内可能启动两次（比如用户手抖点了两下），纯时间戳会撞。
    """
    import secrets

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


def utc_stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Journal:
    """单次 run 的日志写入器。

    用作上下文管理器，退出时关闭句柄：
        with Journal(run_dir, mirror_dir) as journal: ...
    """

    def __init__(self, run_dir: Path, mirror_dir: Path | None = None) -> None:
        self._run_dir = Path(run_dir)
        self._mirror_dir = Path(mirror_dir) if mirror_dir else None
        self._main: TextIO | None = None
        self._mirror: TextIO | None = None
        self._seq = 0
        self._mirror_broken = False

    # -- 生命周期 ---------------------------------------------------------

    def open(self) -> Journal:
        self._run_dir.mkdir(parents=True, exist_ok=True)
        # 已有记录时把 seq 接上再写。崩溃后「恢复执行」会重开同一份 journal，若从 1
        # 重新计数就会出现两条 seq 相同的记录，而撤销的全部语义建立在「按 done 记录
        # 逆序还原」之上——seq 不再是全序，那条语义就无从谈起。
        existing = self.read_records(self._run_dir)
        if existing:
            self._seq = max(record.seq for record in existing)
        self._main = open(
            self._run_dir / JOURNAL_NAME, "a", encoding="utf-8", newline="\n"
        )
        if self._mirror_dir is not None:
            try:
                self._mirror_dir.mkdir(parents=True, exist_ok=True)
                self._mirror = open(
                    self._mirror_dir / JOURNAL_NAME, "a", encoding="utf-8", newline="\n"
                )
            except OSError as exc:
                self._note_mirror_failure(exc)
        return self

    def close(self) -> None:
        for handle in (self._main, self._mirror):
            if handle is None:
                continue
            try:
                handle.close()
            except OSError:
                pass
        self._main = None
        self._mirror = None

    def __enter__(self) -> Journal:
        return self.open()

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- 只读 -------------------------------------------------------------

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    @property
    def mirror_dir(self) -> Path | None:
        return self._mirror_dir

    @property
    def next_seq(self) -> int:
        return self._seq + 1

    @property
    def mirror_healthy(self) -> bool:
        return self._mirror is not None and not self._mirror_broken

    # -- 写入 -------------------------------------------------------------

    def write_manifest(self, manifest: Manifest) -> None:
        """需求 13.1。执行前落盘完整现场。"""
        payload = json.dumps(
            to_jsonable(manifest), ensure_ascii=False, sort_keys=True, indent=2
        )
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write(self._run_dir / MANIFEST_NAME, payload)
        if self._mirror_dir is not None and not self._mirror_broken:
            try:
                self._mirror_dir.mkdir(parents=True, exist_ok=True)
                self._atomic_write(self._mirror_dir / MANIFEST_NAME, payload)
            except OSError as exc:
                self._note_mirror_failure(exc)

    def append(
        self,
        kind: RecordKind,
        *,
        op: object = None,
        src: str | Path | None = None,
        dst: str | Path | None = None,
        size: int | None = None,
        mtime: float | None = None,
        sha256: str | None = None,
        error: str | None = None,
        **extra: object,
    ) -> JournalRecord:
        """追加一条记录并 fsync。返回写出的记录（带分配好的 seq）。"""
        self._seq += 1
        record = JournalRecord(
            seq=self._seq,
            ts=utc_stamp(),
            kind=kind,
            op=op,  # type: ignore[arg-type]
            src=None if src is None else str(src),
            dst=None if dst is None else str(dst),
            size=size,
            mtime=mtime,
            sha256=sha256,
            error=error,
            extra=dict(extra),
        )
        self.append_record(record)
        return record

    def append_record(self, record: JournalRecord) -> None:
        line = json.dumps(to_jsonable(record), ensure_ascii=False, sort_keys=True)
        if self._main is None:
            self.open()
        assert self._main is not None

        self._main.write(line + "\n")
        self._main.flush()
        try:
            os.fsync(self._main.fileno())
        except OSError as exc:  # pragma: no cover - 极少见，但不该让执行中断
            logger.warning("journal fsync 失败: %s", exc)

        self._seq = max(self._seq, record.seq)

        if self._mirror is not None and not self._mirror_broken:
            try:
                self._mirror.write(line + "\n")
                self._mirror.flush()
            except OSError as exc:
                self._note_mirror_failure(exc)

    def _note_mirror_failure(self, exc: BaseException) -> None:
        if not self._mirror_broken:
            logger.warning(
                "根目录镜像日志写入失败，主副本不受影响（撤销仍可用）: %s", exc
            )
        self._mirror_broken = True
        if self._mirror is not None:
            try:
                self._mirror.close()
            except OSError:
                pass
            self._mirror = None

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    # -- 读取 -------------------------------------------------------------

    @staticmethod
    def read_records(run_dir: Path) -> list[JournalRecord]:
        """读日志。坏行被跳过并记 warning，不让一行损坏毁掉整次撤销。"""
        path = Path(run_dir) / JOURNAL_NAME
        if not path.exists():
            return []
        records: list[JournalRecord] = []
        # 只按 "\n" 切，不用 splitlines()：后者还会在 U+0085、U+2028、U+2029 处断行，
        # 而 json.dumps(ensure_ascii=False) 不转义这三个字符。文件名里完全可以含
        # U+2028（Windows 只禁 \ / : * ? " < > | 和控制字符），一旦含了，那条记录就会
        # 被切成两半、解析失败——那个文件的撤销依据就静默丢了。
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").split("\n"), start=1
        ):
            if not line.strip():
                continue
            try:
                records.append(from_jsonable(json.loads(line), JournalRecord))
            except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
                logger.warning("journal 第 %d 行无法解析，已跳过: %s", lineno, exc)
        records.sort(key=lambda r: r.seq)
        return records

    @staticmethod
    def read_manifest(run_dir: Path) -> Manifest | None:
        path = Path(run_dir) / MANIFEST_NAME
        if not path.exists():
            return None
        try:
            return from_jsonable(
                json.loads(path.read_text(encoding="utf-8")), Manifest
            )
        except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            logger.warning("manifest 无法解析: %s", exc)
            return None

    @staticmethod
    def iter_kind(
        records: list[JournalRecord], kind: RecordKind
    ) -> Iterator[JournalRecord]:
        for record in records:
            if record.kind is kind:
                yield record


def mirror_dir_for(root: Path, run_id: str) -> Path:
    """根目录下的镜像位置。需求 13.5。"""
    return Path(root) / MIRROR_DIR_NAME / run_id


def has_unfinished_intents(records: list[JournalRecord]) -> bool:
    """存在 intent 但缺少对应 done/failed。需求 13.7。

    配对依据是 src：同一个文件的 intent 与 done 指向同一源路径。用 src 而非 seq
    是因为 seq 只保证顺序、不表达配对关系。
    """
    intents = {r.src for r in records if r.kind is RecordKind.INTENT and r.src}
    settled = {
        r.src
        for r in records
        if r.kind in (RecordKind.DONE, RecordKind.FAILED, RecordKind.SKIPPED) and r.src
    }
    return bool(intents - settled)


def unfinished_sources(records: list[JournalRecord]) -> list[str]:
    intents = [r.src for r in records if r.kind is RecordKind.INTENT and r.src]
    settled = {
        r.src
        for r in records
        if r.kind in (RecordKind.DONE, RecordKind.FAILED, RecordKind.SKIPPED) and r.src
    }
    return [src for src in intents if src not in settled]
