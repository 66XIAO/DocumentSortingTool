"""目录树构造器。

用嵌套 dict 声明目录结构，一次物化到临时目录：

    build_tree(tmp_path, {
        "报告.pdf": "内容",
        "旧资料": {
            "合同.docx": "x",
            "更深": {"a.txt": "y"},
        },
        ".docsort": {"journal.jsonl": "{}"},
    })

dict 表示目录，str/bytes 表示文件内容。任务 12 的 Hypothesis 生成器产出同样形状的
spec 后交给本模块物化，因此生成器不必关心文件系统细节。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: dict 是目录，str/bytes 是文件内容
TreeSpec = dict[str, "TreeSpec | str | bytes"]


def build_tree(base: Path, spec: TreeSpec) -> Path:
    """把 spec 物化到 base 之下，返回 base。"""
    base.mkdir(parents=True, exist_ok=True)
    for name, value in spec.items():
        target = base / name
        if isinstance(value, dict):
            build_tree(target, value)
        elif isinstance(value, bytes):
            target.write_bytes(value)
        else:
            target.write_text(value, encoding="utf-8")
    return base


def set_hidden(path: Path) -> bool:
    """给路径打上 Windows 隐藏属性。返回是否设置成功。

    非 Windows 或调用失败时返回 False，调用方应改用「以点开头」的命名来表达隐藏，
    使测试在任何平台都有可用的隐藏样本。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        attribute_hidden = 0x2
        result = ctypes.windll.kernel32.SetFileAttributesW(  # type: ignore[attr-defined]
            str(path), attribute_hidden
        )
        return bool(result)
    except Exception:  # noqa: BLE001 - 探测性调用，失败即回落
        return False


def loose_files(directory: Path) -> set[Path]:
    """directory 下的孤立文件（不含任何子目录内容）。"""
    return {p for p in directory.iterdir() if p.is_file()}


def all_paths(directory: Path) -> set[Path]:
    """directory 下的全部路径，含目录与文件。

    用于「范围外不变性」这类断言：比较执行前后的完整路径集合。
    """
    collected: set[Path] = set()
    for current, dirnames, filenames in os.walk(directory):
        here = Path(current)
        for name in dirnames:
            collected.add(here / name)
        for name in filenames:
            collected.add(here / name)
    return collected
