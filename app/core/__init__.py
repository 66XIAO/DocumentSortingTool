"""核心逻辑层：零 Qt 依赖，可独立单测与命令行运行。

需求 18.7 要求本层只依赖标准库与非 Qt 三方库。design.md 中几乎所有可验证条目
（分类确定性、序列化往返、撤销往返、幂等、范围外不变性）都落在本层，因此本层
的测试不需要 QApplication。

模块划分：
    models       全部领域模型与枚举，含 to_jsonable / from_jsonable
    safety       Safety_Guard：根目录准入、路径越界、符号链接策略
    scanner      Scanner + ScanSession：扫描范围状态与增量补扫
    inspector    Content_Extractor：正文前 4KB 提取
    rules        Rule_Engine + Rule_Serializer
    classifiers/ Classifier 协议、管线与各分类器实现
    llm/         Provider 抽象、两阶段 taxonomy、批级缓存
    overrides    OverrideLayer：用户手工调整的叠加
    planner      Planner + TargetAllocator + EmptyDirPredictor
    conflicts    五态冲突判定
    executor     Executor：执行语义 + 空目录清理
    journal      Journal：manifest.json / journal.jsonl 双写 + fsync
    undo         Undo_Manager：撤销 / 重做
    history      History_Manager：run 列表、未收尾检测、保留策略
    fsops        同卷判定、原子重命名、复制校验、回收站、长路径工具
    progress     ProgressThrottle：200ms 批量节流
    cli          无界面入口
"""
