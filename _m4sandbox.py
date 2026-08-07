"""M4 端到端自检：扫描 → 方案 → 模拟运行 → 执行 → 撤销 → 重做 → 再撤销。

不是测试，是一次**真实走一遍**的自检脚本。属性测试跑在 pytest 的 tmp_path 下，
那里有一层守卫放开（system_roots=()）；这个脚本跑在项目目录下、用默认配置路径之外
的本地历史目录，用来确认「把守卫、真实路径、真实 journal 全接上之后仍然对」。

    python _m4sandbox.py

沙盒建在项目目录下的 _m4sandbox/，跑完自动删除。刻意不用 tempfile：
%TEMP% 在本机曾被透明加密驱动干扰过，而这个脚本的结论必须可信。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from app.core.executor import Executor  # noqa: E402
from app.core.history import HistoryManager  # noqa: E402
from app.core.journal import (  # noqa: E402
    Journal,
    mirror_dir_for,
    new_run_id,
    utc_stamp,
)
from app.core.models import (  # noqa: E402
    ConflictPolicy,
    ExecOptions,
    Manifest,
    OverrideSet,
    RecordKind,
    SourceSnapshotEntry,
    Strategy,
)
from app.core.planner import Planner  # noqa: E402
from app.core.safety import SafetyGuard  # noqa: E402
from app.core.scanner import ScanSession  # noqa: E402
from app.core.undo import UndoManager  # noqa: E402

SANDBOX = PROJECT / "_m4sandbox"
ROOT = SANDBOX / "待整理"
HISTORY = SANDBOX / "history"

TREE = {
    "2024年度报告.pdf": "PDF 正文",
    "发票_20240312.pdf": "增值税专用发票 金额 1234.00",
    "采购合同-甲方乙方.docx": "合同正文",
    "张三的简历.docx": "简历正文",
    "屏幕截图 2024-03-01.png": "PNG",
    "假期照片.jpg": "JPG",
    "预算表.xlsx": "表格",
    "readme.md": "# 说明",
    "安装包.exe": "MZ",
    "备份.zip": "PK",
    "来源不明.zzz": "?",
    "空文件.txt": "",
    "旧资料": {  # 未勾选，全程必须一动不动
        "不该被动.docx": "锁死",
        "更深": {"也不该被动.pdf": "锁死"},
    },
    "微信文件": {  # 勾选，其孤立文件参与整理
        "微信图片_001.png": "PNG",
        "微信文档.docx": "DOCX",
        "子相册": {"不该被动.png": "锁死"},
    },
}

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {label}{(' — ' + detail) if detail and not ok else ''}")
    if not ok:
        failures.append(label)


def build(base: Path, spec: dict) -> None:
    base.mkdir(parents=True, exist_ok=True)
    for name, value in spec.items():
        if isinstance(value, dict):
            build(base / name, value)
        else:
            (base / name).write_text(value, encoding="utf-8")


def paths_of(root: Path) -> set[Path]:
    return {
        p.relative_to(root)
        for p in root.rglob("*")
        if ".docsort" not in p.parts
    }


def contents_of(root: Path) -> dict[Path, str]:
    return {
        p.relative_to(root): p.read_text(encoding="utf-8")
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".docsort" not in p.parts
    }


def main() -> int:
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    build(ROOT, TREE)

    guard = SafetyGuard()
    print(f"\n沙盒根目录: {ROOT}")

    # -- 准入 ------------------------------------------------------------
    admission = guard.check_root(ROOT)
    check("准入通过", admission.verdict.value == "allow", str(admission.reason))

    # -- 扫描 ------------------------------------------------------------
    session = ScanSession(ROOT, guard=guard)
    result = session.initial_scan()
    check("根目录孤立文件 12 个", len(result.entries) == 12, str(len(result.entries)))
    check(
        "子文件夹清单 2 个",
        len(result.subfolders) == 2,
        [s.name for s in result.subfolders],
    )
    check(
        "默认不勾选任何子文件夹",
        all(not s.selected for s in result.subfolders),
    )

    wechat = ROOT / "微信文件"
    session.select(wechat)
    entries = session.entries()
    check("勾选微信文件后 14 个条目", len(entries) == 14, str(len(entries)))
    check(
        "勾选不向下继承：子相册的文件未进入范围",
        not any("子相册" in str(e.path) for e in entries),
    )

    # -- 方案 ------------------------------------------------------------
    plan_result = Planner(guard=guard).build(
        entries,
        root=ROOT,
        strategy=Strategy.SMART,
        conflict_policy=ConflictPolicy.AUTO_RENAME,
        selection=session.selection(),
    )
    plan = plan_result.plan
    stats = plan.stats()
    print(
        f"\n  方案：{stats.total_files} 个文件 / {stats.category_count} 个类目"
        f" / 待移动 {stats.pending_move} / 冲突 {stats.conflict_count}"
        f" / 未分类 {stats.unclassified_count}"
    )
    for category in plan.categories:
        count = sum(1 for i in plan.all_items() if i.category_id == category.id)
        print(f"    {'/'.join(category.path_parts):<16} {count} 个")

    check("每个条目都被覆盖一次", len(plan.all_items()) == len(entries))
    check(
        "发票走关键词规则而不是扩展名",
        any(
            i.entry.name.startswith("发票") and "发票" in "/".join(
                plan.category_by_id(i.category_id).path_parts
            )
            for i in plan.items
        ),
    )
    check(
        "目标全部落在根目录之内",
        all(i.target.resolve().is_relative_to(ROOT.resolve()) for i in plan.all_items()),
    )

    before_paths = paths_of(ROOT)
    before_contents = contents_of(ROOT)
    untouched_before = {p for p in before_paths if p.parts[0] == "旧资料"}

    # -- 模拟运行 --------------------------------------------------------
    dry = Executor().run(
        plan,
        journal=None,
        options=ExecOptions(dry_run=True, remove_empty_dirs=True),
        selection=session.selection(),
    )
    check("模拟运行不改动任何路径", paths_of(ROOT) == before_paths)
    check("模拟运行报告覆盖全部条目", dry.total() == len(entries))
    print(f"  模拟运行：成功 {len(dry.succeeded)} / 跳过 {len(dry.skipped)}"
          f" / 失败 {len(dry.failed)} / 预测待删目录 {len(dry.predicted_removed_dirs)}")

    # -- 真实执行 --------------------------------------------------------
    run_id = new_run_id()
    options = ExecOptions(remove_empty_dirs=False)
    with Journal(HISTORY / run_id, mirror_dir_for(ROOT, run_id)) as journal:
        journal.write_manifest(
            Manifest(
                run_id=run_id,
                created_at=utc_stamp(),
                root=str(ROOT),
                strategy=plan.strategy,
                scope=plan.scope,
                selected_subfolders=tuple(str(p) for p in plan.selected_subfolders),
                conflict_policy=options.conflict_policy,
                remove_empty_dirs=options.remove_empty_dirs,
                plan=plan,
                source_snapshot=[
                    SourceSnapshotEntry(
                        path=str(i.entry.path), size=i.entry.size, mtime=i.entry.mtime
                    )
                    for i in plan.all_items()
                ],
                overrides=OverrideSet(),
                app_version="0.1.0",
            )
        )
        report = Executor().run(
            plan, journal=journal, options=options, selection=session.selection()
        )

    print(
        f"\n  执行：成功 {len(report.succeeded)} / 跳过 {len(report.skipped)}"
        f" / 失败 {len(report.failed)} / 用时 {report.elapsed_ms} ms"
    )
    check("执行无失败项", not report.failed, [r.reason for r in report.failed])
    check("三组计数之和等于条目数", report.total() == len(entries))
    check("模拟与真实的三组计数一致", dry.counts() == report.counts())
    check("每个成功项都真的落在目标位置",
          all(Path(r.dst).is_file() for r in report.succeeded))
    check("源位置都已腾空",
          all(not Path(r.src).exists() for r in report.succeeded if r.src != r.dst))
    check("未勾选的旧资料一动未动",
          {p for p in paths_of(ROOT) if p.parts[0] == "旧资料"} == untouched_before)
    check("微信文件/子相册 一动未动",
          (wechat / "子相册" / "不该被动.png").is_file())

    records = Journal.read_records(HISTORY / run_id)
    mirror = Journal.read_records(mirror_dir_for(ROOT, run_id))
    check("journal 主副本与根目录镜像一致", records == mirror)
    check("seq 从 1 连续递增", [r.seq for r in records] == list(range(1, len(records) + 1)))
    done = [r for r in records if r.kind is RecordKind.DONE]
    check("done 记录数等于成功数", len(done) == len(report.succeeded))
    check("manifest 可读回", Journal.read_manifest(HISTORY / run_id) is not None)

    history = HistoryManager(HISTORY)
    summaries = history.list_runs()
    check("历史记录页能列出这次 run", [s.run_id for s in summaries] == [run_id])
    check("状态为已完成", summaries and summaries[0].meta.status.value == "completed")
    check("未收尾检测为空", history.find_unfinished() == [])
    check("可撤销", history.is_undoable(run_id))

    after_paths = paths_of(ROOT)
    after_contents = contents_of(ROOT)

    # -- 撤销 ------------------------------------------------------------
    manager = UndoManager(HISTORY)
    undo = manager.undo(run_id)
    print(f"\n  撤销：{undo.summary()}")
    check("撤销无需人工确认项", not undo.needs_attention,
          [i.reason for i in undo.needs_attention])
    check("撤销无失败项", not undo.failed, [i.reason for i in undo.failed])
    check("撤销后路径集合回到执行前", paths_of(ROOT) == before_paths,
          str(paths_of(ROOT) ^ before_paths))
    check("撤销后文件内容逐字节一致", contents_of(ROOT) == before_contents)
    history.mark_undone(run_id)
    check("已撤销的 run 不可再撤销", not history.is_undoable(run_id))

    # -- 重做 ------------------------------------------------------------
    redo = manager.redo(run_id)
    print(f"  重做：{redo.summary()}")
    check("重做后路径集合回到执行后", paths_of(ROOT) == after_paths)
    check("重做后文件内容一致", contents_of(ROOT) == after_contents)

    # -- 再撤销 ----------------------------------------------------------
    again = manager.undo(run_id)
    print(f"  再撤销：{again.summary()}")
    check("再撤销与首次撤销结果一致", paths_of(ROOT) == before_paths)
    check("再撤销还原条数与首次一致", again.restored == undo.restored)

    # -- 收尾 ------------------------------------------------------------
    print()
    if failures:
        print(f"共 {len(failures)} 项未通过：")
        for label in failures:
            print(f"  - {label}")
    else:
        print("全部自检项通过。")

    shutil.rmtree(SANDBOX, ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
