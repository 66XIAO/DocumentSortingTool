"""Hypothesis 生成器。

生成器覆盖的形状决定了属性测试的价值。design.md「共用的生成器与替身」表列出了
必须覆盖的维度，缺一条就意味着对应属性形同虚设：

    文件名    中文、空格、点开头、超长名、含非法字符
    文件内容  0 字节、多编码文本、二进制、>4KB
    目录形状  空目录、只含子目录、仅含系统生成文件、`.docsort`、深层嵌套
    勾选组合  空集、单个、父子同勾、只勾子不勾父、全选
    配置组合  四种策略 × 三种冲突策略 × include_hidden 真假
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import strategies as st

from app.core.models import (
    ActionKind,
    ClassifyOptions,
    ConflictPolicy,
    DateGranularity,
    ScanOptions,
    Strategy,
)
from tests.fixtures.trees import build_tree

# ---------------------------------------------------------------------------
# 名字
# ---------------------------------------------------------------------------

#: 会被 sanitize_segment 清洗掉的字符（需求 9.8）
ILLEGAL_CHARS = '\\/:*?"<>|'

#: 字符池写成字符串而不是策略：下面还要和非法字符拼接成更大的池
SAFE_CHAR_POOL = "abcXYZ0189 -_中文报告发票合同"

#: 普通文件名主干。
#
# 刻意**不含**非法字符与 Windows 保留设备名：那种文件在 NTFS 上根本创建不出来，
# 生成它们只会让 build_tree 报错，测不到任何被测逻辑。非法字符那一维属于**类目名**
# （由规则给出、经 sanitize_parts 清洗），见下面的 category_segments；保留名的处理
# 由 test_fsops 的例子级测试覆盖。
stems = st.one_of(
    st.text(alphabet=SAFE_CHAR_POOL, min_size=1, max_size=12),
    st.sampled_from(
        [
            "发票_2024",
            "采购合同",
            "张三的简历",
            "Screenshot_20240301",
            "屏幕截图 2024-03-01",
            "image_12",
            ".dotfile",
            "长" * 40,
        ]
    ),
)

extensions = st.sampled_from(
    ["", ".pdf", ".PDF", ".docx", ".xlsx", ".png", ".mp4", ".zip", ".exe", ".py", ".zzz"]
)


@st.composite
def file_names(draw: st.DrawFn) -> str:
    """可在 Windows 上真实创建的文件名。"""
    stem = draw(stems).strip(" .") or "x"
    ext = draw(extensions)
    return f"{stem}{ext}"


#: 类目段：含分隔符与非法字符，用来验证「逐段清洗」而非「整串清洗」
category_segments = st.one_of(
    st.sampled_from(["财务", "文档", "PDF", "发票", "其他", "a/b", 'x:y*z"', "."]),
    st.text(alphabet=SAFE_CHAR_POOL + ILLEGAL_CHARS, min_size=1, max_size=8),
)

category_paths = st.lists(category_segments, min_size=1, max_size=3).map(tuple)


# ---------------------------------------------------------------------------
# 文件内容
# ---------------------------------------------------------------------------

file_contents = st.one_of(
    st.just(""),  # 0 字节
    st.text(alphabet=SAFE_CHAR_POOL, min_size=1, max_size=64),
    st.just("发票代码 011002000111 金额 1234.00"),
    st.just("x" * 5000),  # > 4KB，验证正文截断
    st.binary(min_size=1, max_size=64),
)


# ---------------------------------------------------------------------------
# 目录树
# ---------------------------------------------------------------------------

_SYSTEM_JUNK = st.sampled_from(["desktop.ini", "Thumbs.db"])

#: 目录名同样必须是能真实创建的。`.docsort` 与 `.git` 保留：前者验证排除规则，
#: 后者验证隐藏目录（以点开头）被正确过滤。
dir_names = st.sampled_from(
    ["子目录", "旧资料", "微信文件", "sub", "深", ".docsort", ".git", "空"]
)


@st.composite
def tree_specs(draw: st.DrawFn, max_depth: int = 3) -> dict:
    """嵌套 dict 形式的目录树规格，交给 build_tree 物化。

    刻意包含五种「形状」而不只是随机嵌套：空目录、只含子目录、仅含系统生成文件、
    `.docsort`、深层嵌套。属性 3、37、39 都依赖这些形状才有意义。
    """
    spec: dict = {}

    for name in draw(st.lists(file_names(), max_size=4, unique=True)):
        spec[name] = draw(file_contents)

    if max_depth > 0:
        for name in draw(st.lists(dir_names, max_size=2, unique=True)):
            shape = draw(st.integers(min_value=0, max_value=3))
            if shape == 0:
                spec[name] = {}  # 空目录
            elif shape == 1:
                spec[name] = {draw(_SYSTEM_JUNK): "x"}  # 仅含系统生成文件
            elif shape == 2:
                spec[name] = {"更深": draw(tree_specs(max_depth=max_depth - 1))}
            else:
                spec[name] = draw(tree_specs(max_depth=max_depth - 1))
    return spec


def materialize(base: Path, spec: dict) -> Path:
    return build_tree(base, spec)


# ---------------------------------------------------------------------------
# 配置组合
# ---------------------------------------------------------------------------

strategies_ = st.sampled_from(list(Strategy))
conflict_policies = st.sampled_from(list(ConflictPolicy))
granularities = st.sampled_from(list(DateGranularity))
actions = st.sampled_from([ActionKind.MOVE, ActionKind.COPY])


@st.composite
def scan_options(draw: st.DrawFn) -> ScanOptions:
    return ScanOptions(
        include_hidden=draw(st.booleans()),
        follow_symlinks=False,  # 真实符号链接需要特权，属性测试不依赖它
        excluded_names=frozenset({".docsort"}),
    )


@st.composite
def classify_options(draw: st.DrawFn) -> ClassifyOptions:
    strategy = draw(strategies_)
    return ClassifyOptions(
        strategy=strategy,
        min_confidence=draw(
            st.floats(min_value=0.0, max_value=1.0, allow_nan=False)
        ),
        date_granularity=draw(granularities),
        merge_small_categories=draw(st.booleans()),
        small_category_threshold=draw(st.integers(min_value=1, max_value=5)),
    )


# ---------------------------------------------------------------------------
# 时间戳
# ---------------------------------------------------------------------------

#: 覆盖正常范围与荒谬取值，验证日期分类不会抛异常打断整批
mtimes = st.one_of(
    st.floats(min_value=0, max_value=4_000_000_000, allow_nan=False),
    st.just(0.0),
    st.just(-1.0),
)
