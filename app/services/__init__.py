"""服务层：QThread 包装 core，把 callable 回调翻译成 Qt 信号。

本层是唯一允许同时看见 Qt 与 core 的层，只做线程生命周期与信号桥接，
不放业务判定。

base             WorkerService 基类：QThread 生命周期 + CancelToken 桥接
scan_service     扫描与增量补扫
plan_service     方案生成与重算，持有会话内的 OverrideSet
execute_service  执行
undo_service     撤销 / 重做
llm_service      LLM 调用

执行与撤销刻意单线程顺序：journal 的 seq 必须全序，撤销依赖「done 记录的逆序」
这一语义，并发会让逆序退化成偏序。
"""
