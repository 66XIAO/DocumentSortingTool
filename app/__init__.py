"""DocSorter 应用包。

分层与依赖方向（对应 design.md「分层与依赖方向」）：

    ui  ->  services  ->  core
    ui  ->  config    ->  core
    core ->  标准库 + 非 Qt 三方库

约束：
- `core` 不得 import PySide6、app.services、app.ui、app.config。
  core 需要的配置以窄选项对象（ScanOptions / ClassifyOptions / ExecOptions /
  AIOptions）注入，由 config 层从 Settings 构造，保证 config -> core 单向不成环。
- `core` 与外界只通过入参、返回值、普通 callable 回调交互，内部不出现 Qt Signal。
- `services` 是唯一允许同时看见 Qt 与 core 的层。
- `ui` 只与 services、config 对话，但允许 import core.models 中的纯数据类与枚举。

该约束由 tests/unit/test_layering.py 静态守卫。
"""

__version__ = "0.1.0"
