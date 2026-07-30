"""pytest 全局配置。

注册 Hypothesis profile，对应 design.md「属性测试的实现约定」：
    dev  默认，100 次迭代
    fs   涉及真实文件 I/O 的属性用，50 次迭代
    ci   CI 上用 --hypothesis-profile=ci 提到 200 次

deadline 统一关掉：属性测试里物化临时目录树的耗时不稳定，deadline 会产生
与被测逻辑无关的假失败。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from hypothesis import HealthCheck, settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 支持在未安装为包的情况下直接 `pytest` 运行
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Qt 测试用 offscreen 平台：不依赖桌面会话，也不会在跑测试时闪出窗口。
# 必须在 QApplication 构造之前设置，因此放在 conftest 的模块级。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


settings.register_profile("dev", max_examples=100, deadline=None)
settings.register_profile(
    "fs",
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
settings.register_profile("ci", max_examples=200, deadline=None)
settings.load_profile("dev")
