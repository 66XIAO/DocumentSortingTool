"""配置层。

settings           Settings dataclass 树 + SettingsManager（keyring 集成）
rules_default.yaml 内置规则，随包分发，首次运行释放到 %APPDATA%\\DocSorter\\rules.yaml

本层从 Settings 构造 core 的窄选项对象，使 config -> core 保持单向依赖。
"""
