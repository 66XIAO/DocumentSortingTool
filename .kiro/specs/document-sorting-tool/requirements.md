# 需求文档

## 引言

DocSorter 是一款 Windows 桌面端「文档分类工具」，用于把用户指定目录下散乱的文件按可解释的规则自动归入分类文件夹。技术栈为 Python + PySide6 + PySide6-Fluent-Widgets（Fluent 观感），核心逻辑层不依赖 Qt，可独立单测与命令行运行。

主流程为四步：**选择目录 → 扫描分析 → 生成分类方案并允许用户修改 → 确认后执行**。工具提供 4 种分类策略（按文件类型 / 按时间 / 类型+时间 / 智能），分类结果必须附带命中理由与置信度，让用户在执行前就能判断方案是否可信。

本需求文档把三条产品红线写成可验证条目：

1. **误操作必须可完整复原。** 默认动作是移动，一次误点可能打散上千文件，因此撤销是一等公民：同卷原子重命名、跨卷复制校验后源文件进回收站、双份落盘日志、崩溃后可恢复、撤销前校验文件是否被外部改动、历史时间轴任意条目可撤销。
2. **AI 分类是可选增强，永不成为主流程的单点故障。** `ai.enabled` 默认关闭；任何超时、额度耗尽、非法返回都自动回落到规则引擎并向用户明示。
3. **默认只动根目录下的孤立文件，目录结构默认不被改动。** 子文件夹自身及其内部内容不在默认整理范围内；只有用户逐级手动勾选的子文件夹，其内部的孤立文件才参与整理。范围外的目录及其内容在执行前后保持完全相同的路径集合。根目录内的任何子文件夹默认都不会被删除——无论它是否为空、是否参与整理；子文件夹只在用户完成两层勾选（先勾选该子文件夹参与整理，再勾选「清理整理后变空的子文件夹」并通过警告确认）之后，才可能因本次整理而变空并被清理，且该清理同样可通过撤销恢复。

配套的一条贯穿性约定：**用户在方案预览页做出的每一处手工调整都进入用户覆盖层（override 集合），任何触发方案重算的操作都不会让这些调整丢失。**

### 范围划分

- **MVP（需求 1、2、3、4、9、10、11、12、13、14、15、16、17、18、19）**：扫描范围控制（默认仅根目录孤立文件）、4 种策略生成方案、可视化预览与编辑、手工调整的保留与合并、执行（冲突处理 / 进度 / 日志）、完整撤销。
- **增强（需求 5、6、7、8、20）**：内容级分类（读 PDF/Word 正文）、LLM 语义分组、规则管理 UI 的进阶编辑能力、源侧空目录清理。

## 术语表

- **DocSorter**：本工具的整体应用程序（Windows 桌面端）。
- **Core_Layer**：`app/core` 层，承载扫描、分类、规划、执行、撤销逻辑，不含界面代码。
- **UI**：`app/ui` 层，负责展示与用户交互编排。
- **根目录**：用户选定的待整理目录（整理根目录）。
- **孤立文件**：直接位于某个目录之下、且不包含在该目录任何子目录之中的文件（loose file）。
- **扫描范围**：本次扫描实际收集 FileEntry 的目录集合，由根目录与用户勾选的子文件夹共同决定，由配置项 `scan.scope` 描述。
- **子文件夹清单**：Scanner 在扫描时另行产出的、描述扫描范围可选项的集合，每项对应一个子文件夹及其统计信息。
- **override 集合**（用户覆盖层）：用户在方案预览页做出的全部手工调整的记录集合，条目级 override 以文件的绝对路径为 key，优先级高于任何 Classifier 结果。
- **Safety_Guard**：安全校验子系统，负责根目录准入、路径越界与符号链接策略校验。
- **Scanner**：扫描子系统，按扫描范围收集 FileEntry 集合并产出子文件夹清单。
- **Content_Extractor**：内容提取子系统，读取受支持文档的正文片段。
- **Rule_Engine**：规则引擎，加载并匹配关键词规则与扩展名规则。
- **Rule_Serializer**：规则文件（YAML）的解析与序列化组件。
- **Classifier**：分类器协议，接收一个 FileEntry，返回（类目、置信度、理由）。
- **Classifier_Pipeline**：分类管线，按 priority 由高到低串联多个 Classifier。
- **LLM_Classifier**：基于大语言模型的分类器实现。
- **Provider**：LLM 服务适配层，抽象不同模型服务的调用差异。
- **Planner**：方案生成子系统，产出 SortPlan 并完成冲突预检。
- **Executor**：执行子系统，按 SortPlan 对文件执行动作。
- **Journal**：日志子系统，写 `manifest.json` 与 `journal.jsonl`。
- **Undo_Manager**：撤销子系统，依据 Journal 还原文件操作。
- **History_Manager**：历史记录子系统，管理 run 列表与保留策略。
- **Settings_Manager**：配置子系统，管理配置读写与凭据存取。
- **FileEntry**：单个文件的元数据模型（path、name、ext、size、mtime、is_hidden、depth、mime、text_head、error）。
- **Category**：类目模型（id、path_parts、color、rule_source）。
- **PlanItem**：单个文件的处置决定（entry、category_id、target、action、conflict、confidence、reason、included）。
- **SortPlan**：一次整理的完整方案（root、strategy、categories、items、unclassified）。
- **run**：一次执行过程，由唯一 `run_id` 标识。
- **模拟运行**：产出完整结果报告但不改动任何文件的执行模式（dry-run）。
- **taxonomy**：LLM_Classifier 在第一阶段产出并在第二阶段固定使用的类目集合。
- **`_未分类`**：所有 Classifier 均未给出足够置信度时的兜底类目。

## 需求

### 需求 1：根目录准入与安全红线

**用户故事：** 作为用户，我希望工具在我选错目录时直接拒绝，以便我不会因为一次误选而破坏操作系统或个人配置数据。

#### 验收标准

1. WHEN 用户提交一个根目录路径, THE Safety_Guard SHALL 返回准入判定结果，结果取值为 `allow` 或 `reject` 并附带原因码
2. IF 提交的根目录经 `resolve()` 后等于任一盘符根（例如 `C:\`）, THEN THE Safety_Guard SHALL 返回 `reject` 并给出原因码 `drive_root`
3. IF 提交的根目录经 `resolve()` 后等于或位于 `C:\Windows`、`C:\Program Files`、`C:\Program Files (x86)`、`C:\ProgramData` 或任一 `AppData` 目录之内, THEN THE Safety_Guard SHALL 返回 `reject` 并给出原因码 `system_path`
4. WHEN Planner 产出一个目标路径, THE Safety_Guard SHALL 校验该路径 `resolve()` 后满足 `is_relative_to(根目录)`
5. IF 某个目标路径 `resolve()` 后不满足 `is_relative_to(根目录)`, THEN THE Safety_Guard SHALL 拒绝对应 PlanItem 并将其 conflict 标记为 `path_escape`
6. WHERE 配置项 `scan.follow_symlinks` 取值为 false, THE Scanner SHALL 跳过符号链接与 junction 指向的目录而不递归进入
7. THE DocSorter SHALL 把作用于文件的全部删除语义实现为「移入 Windows 回收站」（作用于空目录的删除语义见需求 20）
8. WHEN 根目录通过准入校验, THE UI SHALL 显示该根目录路径并启用「扫描」动作

### 需求 2：目录扫描与扫描范围控制

**用户故事：** 作为用户，我希望工具默认只整理我指定目录下那些散着的文件，子文件夹要不要一起整理由我逐个勾选决定，以便我既能在几秒内看到方案，也不会有已经归好的文件夹被意外打散。

#### 验收标准

1. THE Settings_Manager SHALL 把配置项 `scan.scope` 的默认值设为 `top_level_only`，该配置项的取值范围为 `top_level_only` 与 `selected_subfolders`
2. WHERE `scan.scope` 取值为 `top_level_only`, WHEN 用户触发扫描, THE Scanner SHALL 使用 `os.scandir` 只把根目录下的孤立文件收集为 FileEntry，并为每个孤立文件产出包含 path、name、ext、size、mtime、is_hidden、depth 的元数据
3. WHERE `scan.scope` 取值为 `top_level_only`, THE Scanner SHALL 把根目录的子文件夹自身及其内部的全部文件与目录排除在 FileEntry 集合之外
4. WHEN 一次扫描完成, THE Scanner SHALL 在 FileEntry 集合之外另行产出子文件夹清单，清单中每项包含子文件夹名、该子文件夹内的孤立文件数、该子文件夹的递归总文件数、该子文件夹的递归总大小
5. WHEN 统计子文件夹的递归总文件数与递归总大小, THE Scanner SHALL 只读取元数据，并把统计所经过的文件排除在 FileEntry 集合之外
6. THE UI SHALL 提供子文件夹选择器，以树形结构逐级展开展示子文件夹清单，并为每一级的每个子文件夹提供独立勾选控件
7. WHEN 用户勾选子文件夹 S, THE Scanner SHALL 把 S 下的孤立文件补充收集为 FileEntry，并把 S 的下级子文件夹保持为未勾选状态直至用户单独勾选
8. WHEN 用户勾选子文件夹 S, THE UI SHALL 在勾选处显示后果说明，明示 S 下的孤立文件将被移出 S 并提升到根目录下的类目目录
9. WHEN 用户勾选至少一个子文件夹, THE Settings_Manager SHALL 把 `scan.scope` 置为 `selected_subfolders`
10. WHEN 用户改变任一子文件夹的勾选状态, THE Scanner SHALL 只对新增范围执行补扫，THE Planner SHALL 按需求 19 保留 override 集合，且 THE UI SHALL 在补扫完成后刷新预览树
11. WHEN 用户取消勾选子文件夹 S, THE Scanner SHALL 把来自 S 的孤立文件从 FileEntry 集合中移除
12. WHILE 扫描进行中, THE Scanner SHALL 运行在工作线程，且 THE UI SHALL 保持对用户输入的响应
13. WHILE 扫描进行中, THE Scanner SHALL 以不高于每 200 毫秒一次的频率批量发送进度信号
14. WHERE `scan.scope` 取值为 `top_level_only` 且根目录下的孤立文件数不超过 5000, WHEN 在本机 SSD 上执行扫描, THE Scanner SHALL 在 1 秒内完成扫描（仅读取元数据）
15. WHERE 用户勾选的子文件夹使待扫描孤立文件总数达到 100000, WHEN 在本机 SSD 上执行扫描, THE Scanner SHALL 在 3 秒内完成扫描（仅读取元数据）
16. IF 某个条目因权限不足、路径过长或 I/O 错误而无法读取元数据, THEN THE Scanner SHALL 在该条目的 error 字段记录错误原因并继续扫描扫描范围内的其余条目
17. WHERE 配置项 `scan.include_hidden` 取值为 false, THE Scanner SHALL 将隐藏文件排除在 FileEntry 集合之外
18. WHEN 用户在扫描进行中触发取消, THE Scanner SHALL 在 500 毫秒内停止扫描并丢弃部分结果
19. THE Scanner SHALL 把根目录下的 `.docsort` 目录及其全部内容排除在 FileEntry 集合与子文件夹清单之外
20. FOR ALL 未被用户勾选的子文件夹，一次执行前后该子文件夹自身及其内部全部文件与目录的绝对路径集合 SHALL 完全一致（范围外不变性）

### 需求 3：分类策略与可插拔分类管线

**用户故事：** 作为用户，我希望在几种现成策略之间选择并看懂每个文件为什么被这样分类，以便我信任这份方案并敢于点执行。

#### 验收标准

1. THE DocSorter SHALL 提供 4 种分类策略：`按文件类型`、`按时间`、`类型+时间`、`智能`
2. THE Settings_Manager SHALL 将 `按文件类型` 作为默认策略
3. WHEN 策略为 `按文件类型`, THE Classifier_Pipeline SHALL 依次求值 filename、extension 两个 Classifier
4. WHEN 策略为 `按时间`, THE Classifier_Pipeline SHALL 依据 FileEntry.mtime 产出单级时间类目，粒度由配置项 `date.granularity`（取值 `年` 或 `年-月`）决定
5. WHEN 策略为 `类型+时间`, THE Classifier_Pipeline SHALL 产出两级类目，第一级为类型类目，第二级为时间类目
6. WHEN 策略为 `智能`, THE Classifier_Pipeline SHALL 依次求值 filename、content、llm、extension 四个 Classifier
7. THE Classifier_Pipeline SHALL 按 priority 由高到低求值，并采纳第一个 confidence 大于或等于配置项 `classify.min_confidence`（默认 0.6）的建议
8. IF 全部 Classifier 给出的 confidence 均小于 `classify.min_confidence`, THEN THE Classifier_Pipeline SHALL 将该 FileEntry 归入 `_未分类` 类目
9. THE Rule_Engine SHALL 赋予关键词规则高于扩展名规则的 priority
10. THE Classifier_Pipeline SHALL 为每条分类结果附带 reason 字符串（例如「扩展名 .pdf → 文档/PDF」）与取值在 0 到 1 之间的 confidence
11. WHEN 一个实现 Classifier 协议的对象以 priority P 注册到 Classifier_Pipeline, THE Classifier_Pipeline SHALL 在后续求值中按 P 所对应的次序调用该对象
12. WHERE 配置项 `classify.merge_small_categories` 取值为 true, WHEN 某个类目最终包含的文件数小于 `classify.small_category_threshold`（默认 3）, THE Planner SHALL 把该类目下的文件改归入 `其他` 类目
13. FOR ALL FileEntry 集合，在配置不变的前提下重复求值 SHALL 产出相同的类目分配结果（确定性）

### 需求 4：规则库的加载、编辑与序列化往返

**用户故事：** 作为进阶用户，我希望能改规则文件把「发票」「合同」这类自己的分类习惯教给工具，以便分类结果贴合我的实际用法。

#### 验收标准

1. WHEN DocSorter 首次启动且 `%APPDATA%\DocSorter\rules.yaml` 不存在, THE Settings_Manager SHALL 把内置 `rules_default.yaml` 释放到该路径
2. THE Rule_Serializer SHALL 把 `rules.yaml` 解析为规则对象集合，每条规则包含 id、type、priority、patterns、category、enabled
3. IF `rules.yaml` 存在 YAML 语法错误或缺少必填字段, THEN THE Rule_Serializer SHALL 返回包含行号与字段名的错误信息，且 THE Rule_Engine SHALL 使用内置默认规则完成本次分类
4. THE Rule_Serializer SHALL 把规则对象集合序列化为 YAML 文本
5. FOR ALL 合法规则对象集合，序列化后再解析 SHALL 产出与原集合等价的规则对象集合（往返一致性）
6. THE Rule_Engine SHALL 内置覆盖以下类目的关键词规则（priority 200）：`财务/发票`（发票、invoice、税票）、`财务/报销`（报销、费用、expense）、`合同协议`（合同、协议、contract、agreement、nda）、`简历`（简历、resume、cv）、`证件资料`（身份证、护照、户口、营业执照）、`截图`（正则 `^(screenshot|屏幕截图|截图|image_?\d+)`）
7. THE Rule_Engine SHALL 内置覆盖以下类目的扩展名规则（priority 100）：`文档/PDF`、`文档/Word`、`文档/表格`、`文档/演示`、`文档/文本`、`电子书`、`图片`、`音视频`、`压缩包`、`安装程序`、`代码`
8. WHEN 用户在规则管理页保存修改, THE Rule_Engine SHALL 在下一次方案生成中使用修改后的规则
9. THE Rule_Engine SHALL 以忽略大小写的方式匹配文件名关键词

### 需求 5：内容级分类

**用户故事：** 作为用户，我希望文件名起得随意时工具也能读一眼正文再分类，以便 `新建文档(3).docx` 这类文件不会全部堆进未分类。

#### 验收标准

1. WHERE 策略为 `智能`, WHEN FileEntry 的扩展名属于 pdf、docx、xlsx、pptx、txt、md 且 size 小于 20MB, THE Content_Extractor SHALL 提取正文前 4KB 写入 FileEntry.text_head
2. IF FileEntry 的 size 大于或等于 20MB 或扩展名不在受支持列表内, THEN THE Content_Extractor SHALL 跳过该文件并保持其 text_head 为空
3. IF 提取过程因文件损坏、加密或格式不符而抛出异常, THEN THE Content_Extractor SHALL 在该 FileEntry 的 error 字段记录原因并继续处理其余文件
4. THE Content_Extractor SHALL 使用最多 8 个工作线程并发提取
5. WHEN FileEntry.text_head 非空, THE by_content Classifier SHALL 在 text_head 上应用关键词规则并产出类目建议
6. WHEN 读取纯文本文件, THE Content_Extractor SHALL 先用 chardet 探测编码再解码

### 需求 6：AI 分类的开关与失败降级

**用户故事：** 作为用户，我希望 AI 分类随时可开可关且出问题时工具照常工作，以便我不必为了整理文件而依赖网络或额度。

#### 验收标准

1. THE Settings_Manager SHALL 把配置项 `ai.enabled` 的默认值设为 false
2. THE UI SHALL 在设置页提供 `ai.enabled` 总开关，并在方案预览页顶部提供作用于同一配置项的即时开关
3. WHERE 未配置任何可用 Provider, THE UI SHALL 把 AI 开关渲染为禁用状态
4. WHILE `ai.enabled` 取值为 false, THE Classifier_Pipeline SHALL 跳过 LLM_Classifier
5. IF LLM 请求超时、返回非 2xx 状态、返回不满足约定 JSON schema 的内容或提示额度耗尽, THEN THE DocSorter SHALL 记录一条错误日志、采用规则引擎结果完成分类，并在 UI 显示「AI 分类不可用，已使用规则分类」
6. FOR ALL LLM 调用失败情形，方案生成 SHALL 仍然产出覆盖全部 FileEntry 的 SortPlan
7. WHEN 用户在方案预览页切换 AI 开关, THE Planner SHALL 按需求 19 定义的重算流程重新生成方案并保留 override 集合，且 THE UI SHALL 在生成完成后刷新预览树
8. THE LLM_Classifier SHALL 把单次请求超时设为配置项 `ai.timeout_seconds`（默认 30 秒）

### 需求 7：LLM Provider 与两阶段调用

**用户故事：** 作为用户，我希望能接入云端兼容服务或完全本地的模型，以便按自己的隐私与成本偏好选择。

#### 验收标准

1. THE DocSorter SHALL 提供 `OpenAICompatProvider`（参数 base_url、api_key、model）与 `OllamaProvider`（参数 host、model）两个 Provider 实现
2. THE Settings_Manager SHALL 通过 keyring 把 api_key 写入 Windows 凭据管理器
3. THE Settings_Manager SHALL 在写入配置文件时以 keyring 引用键代替 api_key 明文
4. WHEN 用户在设置页触发「测试连通性」, THE Provider SHALL 发起一次最小请求并在 10 秒内返回成功结果或失败原因
5. WHEN LLM_Classifier 开始工作, THE LLM_Classifier SHALL 先抽样不超过 300 个文件摘要请求模型产出 taxonomy
6. THE LLM_Classifier SHALL 约束 taxonomy 的类目数不超过 12 且层级深度不超过 2
7. WHEN taxonomy 已确定, THE LLM_Classifier SHALL 把全部文件摘要按每批 80 至 120 条分批发送，并在提示中要求模型只能从 taxonomy 中选择类目或返回 `unknown`
8. THE LLM_Classifier SHALL 以 temperature 等于 0 且要求 JSON schema 结构化输出的方式调用模型
9. IF 模型返回的类目不属于 taxonomy, THEN THE LLM_Classifier SHALL 把该条目的 confidence 置为 0 并交由后续 Classifier 处理
10. THE LLM_Classifier SHALL 使用编辑距离与同义词表把相近类目名归并为同一类目（例如发票、发票单、电子发票、票据归并为一个类目）
11. IF 归并后的类目数仍超过 12, THEN THE LLM_Classifier SHALL 按文件数从少到多把类目合并进 `其他`，直至类目数不超过 12

### 需求 8：AI 的隐私、成本与可解释性

**用户故事：** 作为用户，我希望清楚知道有哪些信息被发出去、要花多少次请求、哪些结果是 AI 给的，以便我在可控范围内使用这项能力。

#### 验收标准

1. WHERE 隐私级别为 `仅元数据`（默认）, THE LLM_Classifier SHALL 只发送文件名、扩展名、大小、修改时间以及去掉根目录前缀的相对路径
2. WHERE 隐私级别为 `元数据+正文前500字`, THE LLM_Classifier SHALL 额外发送 text_head 的前 500 个字符
3. WHEN 用户把隐私级别切换为 `元数据+正文前500字`, THE UI SHALL 要求用户显式勾选确认后才保存该设置
4. THE LLM_Classifier SHALL 在构造请求内容时以相对路径替换绝对路径，使发送内容不包含根目录前缀
5. WHEN 一批文件摘要的哈希已存在于本地 SQLite 缓存, THE LLM_Classifier SHALL 复用缓存结果并跳过该批请求
6. WHERE `ai.enabled` 取值为 true, WHEN 方案尚未生成, THE UI SHALL 显示待处理文件总数与预估请求次数（例如「1,240 个文件，约 13 次请求」）
7. WHEN PlanItem 的类目由 LLM_Classifier 分配, THE UI SHALL 在该条目上显示 AI 角标
8. WHEN PlanItem 的 confidence 小于 0.6, THE Planner SHALL 把该条目的 included 置为 false
9. FOR ALL 缓存命中情形，缓存返回的类目分配 SHALL 与首次调用产出的类目分配一致（缓存往返一致性）

### 需求 9：方案生成与冲突预检

**用户故事：** 作为用户，我希望所有冲突和风险在执行前就被摊开，以便执行阶段不出意外。

#### 验收标准

1. WHEN 分类完成, THE Planner SHALL 产出包含 root、strategy、categories、items、unclassified 的 SortPlan
2. THE Planner SHALL 为每个 PlanItem 计算形如 `根目录\类目路径\文件名` 的 target 绝对路径与 action，action 取值为 `move`、`copy` 或 `skip`，且来自用户勾选子文件夹的 FileEntry 采用同一 target 构造规则
3. THE Planner SHALL 为每个 PlanItem 给出确定的 conflict 取值，取值范围为 `none`、`exists`、`path_too_long`、`locked`、`path_escape`
4. WHERE 冲突策略为 `自动重命名`（默认）, WHEN 目标路径已存在同名文件, THE Planner SHALL 生成形如 `文件 (2).pdf` 的不冲突名称并把 conflict 标记为 `exists`
5. WHERE 冲突策略为 `跳过`, WHEN 目标路径已存在同名文件, THE Planner SHALL 把该 PlanItem 的 action 置为 `skip`
6. WHERE 冲突策略为 `覆盖`, THE UI SHALL 要求用户二次确认后才允许启动执行
7. IF 目标绝对路径长度超过 259 个字符, THEN THE Planner SHALL 把 conflict 标记为 `path_too_long` 并提示用户缩短类目名
8. THE Planner SHALL 把类目名中的 `\ / : * ? " < > |` 字符替换为下划线
9. IF 源文件无法以写入方式打开（被占用或只读）, THEN THE Planner SHALL 把 conflict 标记为 `locked`
10. IF 目标位于其他卷且该卷剩余空间小于待复制文件总大小的 1.1 倍, THEN THE Planner SHALL 阻止执行并提示剩余空间不足
11. THE Planner SHALL 把类目目录创建为根目录的直接子文件夹，使类目目录在 `scan.scope` 取值为 `top_level_only` 时位于扫描范围之外，已归类文件因此不进入后续扫描的 FileEntry 集合
12. WHEN FileEntry 的当前父目录已等于其目标类目目录, THE Planner SHALL 把该 PlanItem 的 action 置为 `skip`（该条目作为兜底，覆盖用户勾选类目目录参与整理的情形）
13. FOR ALL SortPlan，在其执行完成后以相同配置与相同扫描范围再次生成方案 SHALL 产出全部 action 均为 `skip` 的方案（幂等性）

### 需求 10：方案预览与编辑

**用户故事：** 作为用户，我希望在执行前逐条看清、逐条改动这份方案，以便把工具的判断修正成我想要的结果。

#### 验收标准

1. THE UI SHALL 在方案预览页顶部显示统计卡，含文件总数、类目数、待移动数、冲突数、未分类数
2. THE UI SHALL 在左栏显示类目列表，并提供改名、换色、合并、新建、删除动作
3. WHEN 用户删除某个类目, THE Planner SHALL 把该类目下的文件改归入 `_未分类` 类目
4. THE UI SHALL 在中栏显示目标结构树，并提供 `整理前` 与 `整理后` 两个视图的切换
5. WHEN PlanItem 的 conflict 取值不为 `none`, THE UI SHALL 在对应树节点显示橙色角标
6. WHEN PlanItem 的目标文件名与源文件名不同, THE UI SHALL 在该行显示 `旧名 → 新名`
7. WHEN 用户在中栏选中一个文件, THE UI SHALL 在右栏显示原路径、目标路径、命中规则、大小与修改时间
8. WHERE 选中文件为图片, THE UI SHALL 在右栏显示该文件的缩略图
9. WHERE 选中文件的 text_head 非空, THE UI SHALL 在右栏显示正文前若干行
10. WHEN 用户取消勾选若干条目, THE Executor SHALL 在执行时跳过 included 取值为 false 的条目
11. WHEN 用户把文件拖拽到另一个类目节点, THE Planner SHALL 更新该 PlanItem 的 category_id 与 target
12. THE UI SHALL 提供按文件名搜索过滤，以及 `只看冲突` 与 `只看未分类` 两个筛选开关
13. THE UI SHALL 使用 QTreeView 搭配自定义 QAbstractItemModel 承载预览树并按需加载子节点
14. WHEN 预览树承载 100000 个条目, THE UI SHALL 使滚动与展开操作的响应时间保持在 100 毫秒以内（该规模仅在用户勾选大量子文件夹时出现）
15. WHEN 用户执行本需求中的任一编辑动作（类目改名、换色、合并、新建、删除，改变条目 included 勾选状态，把条目拖拽到另一类目）, THE Planner SHALL 按需求 19 把该动作记入 override 集合
16. THE UI SHALL 在方案预览页提供子文件夹选择器入口，并显示当前扫描范围包含的子文件夹数量

### 需求 11：执行前的防护与可取消

**用户故事：** 作为用户，我希望在真正动文件之前有多道拦截，以便一次误点不会立刻造成上千文件被打散。

#### 验收标准

1. THE Settings_Manager SHALL 把默认动作设为 `移动`
2. WHEN 用户首次对某个根目录触发执行, THE Executor SHALL 先运行一次模拟运行并由 UI 展示结果，之后才允许真实执行
3. WHEN 用户触发执行, THE UI SHALL 显示确认对话框，内容包含待移动文件数、目标文件夹数与可完整撤销的说明（例如「将移动 1,240 个文件到 8 个文件夹，此操作可完整撤销」）
4. WHEN 用户在确认对话框中确认, THE UI SHALL 提供 3 秒的取消窗口并在此期间显示取消按钮
5. IF 用户在 3 秒取消窗口内点击取消, THEN THE Executor SHALL 放弃本次执行并保持全部文件位置不变
6. WHILE 执行进行中, THE UI SHALL 提供「停止」动作
7. WHEN 用户在执行中触发停止, THE Executor SHALL 在当前单个文件操作完成后停止后续操作，且 THE UI SHALL 提供「回滚已完成部分」动作
8. WHEN 用户触发模拟运行, THE Executor SHALL 产出与真实执行结构相同的结果报告并保持全部文件位置不变

### 需求 12：执行语义与数据完整性

**用户故事：** 作为用户，我希望移动过程在物理层面就不可能丢数据，以便我不用在事后自己核对文件是否还在。

#### 验收标准

1. WHERE 源路径与目标路径位于同一卷, THE Executor SHALL 使用 `os.replace` 原子重命名完成移动
2. WHERE 源路径与目标路径位于不同卷, THE Executor SHALL 依次执行复制到目标、校验大小与内容哈希一致、把源文件移入回收站三个步骤
3. IF 跨卷复制后的大小或内容哈希与源文件不一致, THEN THE Executor SHALL 删除已复制的目标文件、保留源文件并把该条目标记为 `failed`
4. WHERE 冲突策略为 `覆盖`, WHEN 目标路径已存在同名文件, THE Executor SHALL 先把被覆盖文件移入回收站并在 journal 中记录该动作
5. WHEN 目标类目目录不存在, THE Executor SHALL 创建该目录并在 journal 中记录一条 `created_dir`
6. IF 单个文件操作抛出异常, THEN THE Executor SHALL 把该条目标记为 `failed` 并继续处理其余条目
7. WHILE 执行进行中, THE Executor SHALL 运行在工作线程并以不高于每 200 毫秒一次的频率向 UI 批量发送进度
8. FOR ALL 成功完成的移动操作，目标文件的大小与内容哈希 SHALL 与执行前源文件的大小与内容哈希一致（内容不变性）

### 需求 13：操作日志与崩溃恢复

**用户故事：** 作为用户，我希望即使执行中断电或崩溃，工具也知道做到了哪一步，以便我能选择继续或者全部退回。

#### 验收标准

1. WHEN 一次执行开始, THE Journal SHALL 在 `%APPDATA%\DocSorter\history\<run_id>\` 下写入 `manifest.json`，内容包含完整 SortPlan、源树快照（每个条目的 path、size、mtime）、`scan.scope` 取值与用户勾选的子文件夹清单
2. THE Journal SHALL 在同一 run 目录下写入 `journal.jsonl`
3. WHEN 即将执行一个文件操作, THE Journal SHALL 先追加一条 `intent` 记录并对文件描述符执行 fsync
4. WHEN 一个文件操作结束, THE Journal SHALL 追加一条 `done` 或 `failed` 记录并对文件描述符执行 fsync
5. THE Journal SHALL 在根目录下的 `.docsort\` 目录中写入该 run 记录的镜像副本
6. THE Undo_Manager SHALL 以 `%APPDATA%\DocSorter\history\` 下的副本作为撤销所依据的主副本
7. WHEN DocSorter 启动, THE History_Manager SHALL 检查是否存在含 `intent` 记录但缺少对应 `done` 或 `failed` 记录的 run
8. IF 存在未收尾的 run, THEN THE UI SHALL 显示对话框并提供「恢复执行」「全部撤销」「忽略」三个选项
9. FOR ALL journal 记录对象，序列化为 JSONL 行后再解析 SHALL 产出等价的记录对象（往返一致性）
10. FOR ALL SortPlan，写入 `manifest.json` 后再读取 SHALL 产出等价的 SortPlan（往返一致性）

### 需求 14：撤销与重做

**用户故事：** 作为用户，我希望一键把整理彻底退回原状，以便我敢于尝试自动分类而不担心后悔。

#### 验收标准

1. THE UI SHALL 在执行结果页提供「撤销本次整理」动作并把 Ctrl+Z 绑定到该动作
2. WHEN 用户触发撤销, THE Undo_Manager SHALL 按 journal 中 `done` 记录的逆序还原每个文件操作
3. WHERE 原操作为同卷 `os.replace` 移动, THE Undo_Manager SHALL 通过反向 `os.replace` 还原该文件
4. WHERE 原操作为跨卷复制并把源文件移入回收站, THE Undo_Manager SHALL 把目标文件复制回源路径、校验一致后删除目标副本
5. WHEN 准备还原某个文件, THE Undo_Manager SHALL 先比对该文件当前的 size 与 mtime 和 journal 记录值
6. IF 当前 size 或 mtime 与 journal 记录值不一致, THEN THE Undo_Manager SHALL 跳过该文件、把它列入「需人工确认」列表，并提供源与目标的并排对比信息
7. THE Undo_Manager SHALL 只删除 journal 中标记为 `created_dir` 且当前为空的目录
8. WHEN journal 中存在 `removed_dir` 记录, THE Undo_Manager SHALL 按需求 20 定义的重建语义还原这些目录（`created_dir` 与 `removed_dir` 属于两类记录，撤销语义相反）
9. THE Undo_Manager SHALL 为撤销过程本身写入独立的 journal 记录
10. WHEN 一次撤销完成, THE UI SHALL 提供「重做」动作
11. WHEN DocSorter 在执行进程被强制终止后重启且用户选择「全部撤销」, THE Undo_Manager SHALL 依据 journal 还原全部 `done` 操作，并把停留在 `intent` 状态的条目列入「需人工确认」列表
12. FOR ALL 成功完成且文件未被外部修改的 run，执行后撤销 SHALL 使每个受影响文件回到其执行前的绝对路径（撤销往返性）
13. FOR ALL 已撤销的 run，撤销后重做再撤销 SHALL 产出与首次撤销相同的文件树状态（幂等性）

### 需求 15：历史记录与保留策略

**用户故事：** 作为用户，我希望能翻看过去每次整理并撤销其中任意一次，以便我在几天后发现问题时仍有退路。

#### 验收标准

1. THE UI SHALL 提供历史记录页，以时间轴形式列出每条 run 的时间、根目录、文件数、策略、撤销按钮与撤销状态
2. THE History_Manager SHALL 支持对列表中任意一条未撤销的 run 触发撤销
3. THE History_Manager SHALL 默认保留最近 20 条 run 且保留期为 30 天
4. WHEN run 数量超过保留上限或某条 run 超出保留期, THE History_Manager SHALL 删除最旧的 run 目录
5. THE Settings_Manager SHALL 提供保留条数与保留天数两个可修改配置项
6. WHEN 某条 run 已被撤销, THE UI SHALL 把该条目标记为「已撤销」并禁用其撤销按钮

### 需求 16：执行结果页与报告导出

**用户故事：** 作为用户，我希望执行完能看到一份清楚的成绩单，以便确认哪些成功、哪些跳过、哪些失败。

#### 验收标准

1. WHILE 执行进行中, THE UI SHALL 显示进度条与实时日志表
2. WHEN 执行结束, THE UI SHALL 按成功、跳过、失败三组展示结果报告并显示各组计数
3. THE UI SHALL 提供「打开目录」动作以在文件资源管理器中定位根目录
4. WHEN 用户触发导出, THE UI SHALL 把本次结果导出为 CSV 文件，字段包含源路径、目标路径、动作、结果、理由
5. FOR ALL 结果报告，导出 CSV 的数据行数 SHALL 等于成功、跳过、失败三组计数之和

### 需求 17：界面结构与视觉规范

**用户故事：** 作为用户，我希望界面清爽好用且与 Windows 11 观感一致，以便我愿意长期把它当成日常工具。

#### 验收标准

1. THE UI SHALL 提供侧边导航，包含整理流程、历史记录、规则管理、设置四个入口
2. THE UI SHALL 以选择目录、扫描分析、方案预览、执行结果四个步骤组织整理主流程
3. THE DocSorter SHALL 基于 PySide6 与 PySide6-Fluent-Widgets 构建界面
4. THE UI SHALL 使用 `#2563EB` 作为主色、8px 圆角，以及 4、8、12、16、24 的间距栅格
5. THE UI SHALL 使用 `#16A34A` 表示成功状态、`#F59E0B` 表示冲突状态、`#DC2626` 表示失败状态
6. THE UI SHALL 使用 Segoe UI 与微软雅黑 UI 作为字体族
7. WHEN 系统主题为深色或浅色, THE UI SHALL 采用与系统一致的主题
8. THE UI SHALL 为类目 chip 从 10 色柔和色板中轮转取色
9. WHEN 当前页面无数据可展示, THE UI SHALL 显示空态视图并给出下一步动作提示
10. WHILE 扫描进行中, THE UI SHALL 显示骨架屏
11. IF 某个步骤发生错误, THEN THE UI SHALL 显示错误态视图，含错误摘要与重试动作

### 需求 18：配置、依赖与打包

**用户故事：** 作为用户，我希望拿到一个双击就能用的 exe，同时规则和配置放在我能改到的位置，以便安装和自定义都不折腾。

#### 验收标准

1. THE Settings_Manager SHALL 把配置持久化到 `%APPDATA%\DocSorter\settings.yaml`
2. IF `settings.yaml` 缺失或存在非法字段, THEN THE Settings_Manager SHALL 使用默认值填充并重写该文件
3. FOR ALL 合法配置对象，写入 `settings.yaml` 后再读取 SHALL 产出等价的配置对象（往返一致性）
4. THE DocSorter SHALL 使用 LGPL 授权的 PySide6 作为 Qt 绑定
5. THE DocSorter SHALL 在 `requirements.txt` 中固定 PySide6、PySide6-Fluent-Widgets、pyyaml、send2trash、chardet、keyring、httpx、pypdf、python-docx、openpyxl、python-pptx 的版本号
6. THE DocSorter SHALL 通过 PyInstaller 打包为 Windows 可执行文件，并以 `--add-data` 把 `rules_default.yaml` 纳入包内
7. THE Core_Layer SHALL 只依赖标准库与非 Qt 第三方库，以支持无界面单测与命令行运行
8. THE Settings_Manager SHALL 把 `scan.scope` 与 `cleanup.remove_empty_dirs` 纳入 `settings.yaml` 的持久化字段

### 需求 19：用户手工调整的保留与合并

**用户故事：** 作为用户，我希望我在方案预览页做过的手工调整不会因为切换开关、改规则或重新扫描而消失，以便我不必反复重做同样的修改。

#### 验收标准

1. WHEN 用户在方案预览页执行类目改名、类目换色、类目合并、类目新建、类目删除、改变条目 included 勾选状态或把条目拖拽到另一类目, THE Planner SHALL 把该修改记入 override 集合
2. THE Planner SHALL 以文件的绝对路径作为条目级 override 的 key
3. THE Planner SHALL 在下列每个场景重算方案并保留 override 集合：切换 AI 开关、切换分类策略、修改规则库、重新扫描、改变扫描范围（勾选或取消勾选子文件夹）、切换冲突策略
4. WHEN 需要重算方案, THE Planner SHALL 先由 Classifier_Pipeline 产出基础方案，再把 override 集合叠加覆盖到基础方案之上
5. THE Planner SHALL 赋予 override 高于任何 Classifier 结果的优先级
6. IF override 引用的类目在新的基础方案中不存在, THEN THE Planner SHALL 以该类目原有的名称与颜色重建该类目
7. IF override 引用的文件在新的扫描结果中不存在, THEN THE Planner SHALL 丢弃该条 override，且 THE UI SHALL 显示被丢弃的 override 条数
8. WHEN 一次重算完成, THE UI SHALL 显示「已保留 N 项手工调整」，其中 N 为重算后仍然生效的 override 条数
9. THE UI SHALL 在方案预览页提供「清除全部手工调整」动作
10. WHEN 用户触发「清除全部手工调整」, THE UI SHALL 要求用户二次确认，并在确认后清空 override 集合并重算方案
11. WHEN 一次执行开始, THE Journal SHALL 把本次执行生效的 override 集合写入 `manifest.json`
12. FOR ALL override 集合，序列化后再解析 SHALL 产出等价的 override 集合（往返一致性）
13. FOR ALL FileEntry 集合与 override 集合，重算方案 SHALL 使被 override 覆盖的每个条目保持 override 指定的类目与 included 状态（override 优先性）
14. FOR ALL override 集合，在配置与扫描范围不变的前提下连续两次重算 SHALL 产出相同的类目分配与相同的 included 状态（重算稳定性）

### 需求 20：源侧空目录清理

**用户故事：** 作为用户，我希望文件被移走后剩下的空文件夹删不删由我决定，以便根目录既能彻底清爽，也不会在我不知情的情况下丢掉原有的文件夹结构。

#### 验收标准

1. THE Settings_Manager SHALL 把配置项 `cleanup.remove_empty_dirs` 的默认值设为 false
2. THE UI SHALL 在方案预览页底部操作条提供作用于 `cleanup.remove_empty_dirs` 的开关，且该开关的文案为「清理整理后变空的子文件夹」
3. WHEN 用户把 `cleanup.remove_empty_dirs` 由 false 切换为 true, THE UI SHALL 显示警告对话框，对话框内容包含以下三项说明：只有在本次整理中被移空的子文件夹会被删除、原有文件夹结构会因此改变、该删除动作可通过撤销恢复；并要求用户显式确认
4. IF 用户在该警告对话框中选择取消, THEN THE Settings_Manager SHALL 把 `cleanup.remove_empty_dirs` 保持为 false
5. THE DocSorter SHALL 要求两层勾选同时成立才可能删除任何目录：第一层为用户按需求 2.7 勾选某个子文件夹参与整理，第二层为用户按本需求第 3 条勾选「清理整理后变空的子文件夹」并通过警告确认
6. THE Executor SHALL 只把用户已勾选参与整理的子文件夹纳入空目录清理的候选范围
7. WHERE `cleanup.remove_empty_dirs` 取值为 true, WHEN 用户触发执行, THE UI SHALL 在确认对话框中显示预计被删除的空目录数量与完整清单
8. WHERE `cleanup.remove_empty_dirs` 取值为 true, WHEN 全部文件操作结束, THE Executor SHALL 删除同时满足以下四个条件的目录：(a) 位于根目录之内；(b) 该目录在本次 run 中有文件被移出，即该目录是本次 run 中至少一条 `done` 移动记录的源父目录（该目录因本次整理而变空）；(c) 在全部文件操作结束后该目录既不含任何文件也不含任何子目录；(d) 该目录不是根目录本身
9. IF 某个目录在本次 run 开始前已经为空且本次 run 未从该目录移出任何文件, THEN THE Executor SHALL 保留该目录（执行前就已为空的目录不属于「整理后变空」，不在删除范围之内）
10. WHERE `scan.scope` 取值为 `top_level_only`, THE Executor SHALL 保留根目录内的全部子文件夹（此时没有任何文件从子文件夹中被移出，不存在因本次整理而变空的目录）
11. THE Executor SHALL 把「该目录不含任何文件且不含任何子目录」作为判定目录为空的唯一口径
12. IF 某个目录仅包含 `desktop.ini`、`Thumbs.db` 等系统生成文件, THEN THE Executor SHALL 判定该目录为非空并保留该目录
13. THE Executor SHALL 保留未被用户勾选参与整理的子文件夹及其内部的全部目录，使需求 2.20 的范围外不变性在 `cleanup.remove_empty_dirs` 取值为 true 时同样成立
14. THE Executor SHALL 在全部配置组合下保留根目录本身
15. THE Executor SHALL 以 `os.rmdir` 删除空目录，该动作不涉及文件数据，可撤销性由 `removed_dir` 记录保证
16. WHEN Executor 删除一个空目录, THE Journal SHALL 追加一条包含该目录绝对路径的 `removed_dir` 记录并对文件描述符执行 fsync
17. WHERE `cleanup.remove_empty_dirs` 取值为 false, THE Executor SHALL 保留根目录内的全部目录
18. WHEN 撤销一次 run, THE Undo_Manager SHALL 按 `removed_dir` 记录的逆序重建每个被删除的空目录
19. THE Undo_Manager SHALL 对 `created_dir` 记录采用删除语义、对 `removed_dir` 记录采用重建语义
20. WHEN 模拟运行且 `cleanup.remove_empty_dirs` 取值为 true, THE Executor SHALL 在结果报告中列出将被删除的空目录并保持全部目录存在
21. FOR ALL 开启空目录清理并成功完成的 run，执行后撤销 SHALL 使全部被删除的空目录重新存在于其原绝对路径（空目录撤销恢复性）
