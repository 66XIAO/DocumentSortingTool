# 实施计划

## 环境约束：项目必须放在 C:\dev 下，不能放在 Documents 下

本机装有透明文件加密（防泄密）驱动，作用范围覆盖 `C:\Users\111\Documents`。已实测确认：

- `C:\Users\111\Documents\DocumentSortingTool\app\__init__.py` 被 `python.exe` 读到的前 16 字节是
  `b'%TSD-Header-###%'`，文件被填充到 8192 字节且含约 3800 个 null 字节
- 同一文件由 IDE 读取是完整明文 → IDE 进程在加密驱动白名单内，`python.exe` 不在
- 因此在该路径下 `pytest` 直接失败：`SyntaxError: source code string cannot contain null bytes`
- 对照实验：`C:\dev\enctest\t.py` 由 `python.exe` 读取是明文（`b'print(123) \r\n'`）并能正常执行

结论与约定：

1. **项目根目录必须位于 `C:\dev\DocumentSortingTool`**，不得放在 `Documents`、`Desktop` 等受策略覆盖的路径下
2. **不得用命令行工具（PowerShell / robocopy / xcopy）在受保护路径与非保护路径之间复制项目文件**。这些进程不在白名单，读到的是密文，复制结果是无法解密的垃圾。搬迁只能用文件资源管理器（explorer.exe 通常在白名单内）或加密客户端自带的解密/外发功能
3. 若后续新增目录或迁移路径，先用
   `python -c "print(open(r'<路径>','rb').read()[:16])"`
   验证该路径下 `python.exe` 能读到明文，再动手

## 关于本清单的约定

- 任务顺序即执行顺序。同一里程碑内的任务存在依赖关系，不要跳跃执行。
- 每条任务末尾的 `_需求:_` 指向 `requirements.md` 的验收标准编号，`_属性:_` 指向 `design.md` 正确性属性编号。
- 标注 **【需用户手动执行】** 的任务无法由编码 agent 完成（本工作会话终端不可用），必须由用户在 Anaconda Prompt 中执行并把结果反馈回来。
- 严格遵循「先 core 后 ui」：`app/core` 零 Qt 依赖，其属性测试不需要 `QApplication`。
- 属性测试遵循 `design.md`「属性测试的实现约定」：一属性一测试函数，首行注释固定为
  `# Feature: document-sorting-tool, Property N: <属性正文>`
- 每个里程碑的最后一条任务是该阶段属性测试的收口，该任务完成时本阶段属性必须全绿才能进入下一里程碑。

### 设计文档的一处编号勘误

`design.md`「实施策略与里程碑」表中，属性 11 同时出现在 M2 与 M5 两行。属性 11 校验的是正文提取的门槛、上限与编码正确性（需求 5.1、5.2、5.5、5.6），依赖 M5 才实现的 `ContentExtractor`。本清单按 **属性 11 归 M5** 处理，M2 的应通过属性为 2、7、8、9、10、16、17。

---

## M1 骨架与扫描

交付：分层骨架、`SafetyGuard`、`ScanSession`、`SettingsManager`、UI 框架与侧边导航、子文件夹选择器。
本阶段应通过属性：1、3、4、5、30、40、42。

- [x] 1. 【需用户手动执行】确认 Python 版本并安装依赖
  - **已完成。** 全部依赖装入 `SortingTool`，验证通过：`from PySide6.QtWidgets import QApplication` + `import qfluentwidgets` 正常，qfluentwidgets 版本 1.11.2
  - **已确认：`SortingTool` 环境为 Python 3.12.13，pip 26.1.2。** 无需退版，3.12 上全部依赖均有 wheel
  - **首次安装尝试已失败**：PyPI 直连下载 `pyside6_addons`（168.8MB）时连接中断
    （`ProtocolError: Connection broken: IncompleteRead(107902852 bytes read, 60913456 more expected)`），该批依赖全部未安装
  - 重装时必须加镜像与重试参数，否则大包下不完：
    `-i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 60 --retries 10`
  - 运行期依赖（`PySide6` 与 `PySide6-Essentials` 二选一，见任务 1a 的取舍）：
    `python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 60 --retries 10 "PySide6-Essentials>=6.8" "PySide6-Fluent-Widgets==1.11.2" pyyaml send2trash chardet keyring httpx pypdf python-docx openpyxl python-pptx`
  - 开发期依赖：
    `python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 60 --retries 10 pytest pytest-qt pytest-benchmark hypothesis pyinstaller`
  - `PySide6-Fluent-Widgets` 显式钉 `==1.11.2`：镜像源上该包常年落后，钉版本能让镜像过期时直接报错，而不是静默装上旧版导致 `ImportError: cannot import name ... from 'qfluentwidgets'`。若镜像报找不到 1.11.2，单独用 `-i https://pypi.org/simple` 装这一个包（约 4MB，直连没问题）
  - 只能安装 `PyQt-Fluent-Widgets` / `PyQt6-Fluent-Widgets` / `PySide2-Fluent-Widgets` / `PySide6-Fluent-Widgets` 中的一个，四者顶层包名同为 `qfluentwidgets`，共存会互相覆盖
  - 安装完成后执行 `python -m pip list --format=freeze` 并把完整输出反馈回来，供任务 13 回填版本号
  - _需求: 18.4, 18.5_

- [ ] 1a. 【需用户决策】确定 Qt 绑定包的粒度
  - `PySide6` 是元包，会拉入 `PySide6-Essentials`（约 80MB）+ `PySide6-Addons`（168.8MB）。首次安装失败正是卡在 Addons 这个单文件上
  - DocSorter 只用到 QtCore / QtGui / QtWidgets / QtSvg，全部在 Essentials 内。Addons 装的是 Qt3D、QtCharts、QtWebEngine、QtMultimedia、QtQuick 等，本项目一个都不用
  - 改用 `PySide6-Essentials` 的收益：下载量从约 250MB 降到约 80MB，消除单个大文件的下载失败点，PyInstaller 产物同步瘦身（与任务 59 的「裁剪未使用 Qt 模块」同向）
  - 风险：`qfluentwidgets` 的多媒体组件位于独立子模块 `qfluentwidgets.multimedia`，需要 QtMultimedia（属 Addons）。只要不 import 该子模块就不受影响，本项目的组件清单不含多媒体控件
  - 待验证项：`PySide6-Fluent-Widgets` 或 `PySideSix-Frameless-Window` 是否硬依赖 `PySide6` 元包。首次安装日志中二者打印出的依赖分别只有 `PySideSix-Frameless-Window>=0.8.0` / `darkdetect` 与 `pywin32`，未出现 `PySide6`，但 pip 会省略打印已在收集中的依赖，故不能据此断定。执行任务 1 的命令时若 pip 仍开始拉 `PySide6_Addons`，即说明存在传递性硬依赖，此时退回 `"PySide6>=6.8"`
  - 该项影响需求 18.5 的依赖清单；若选定 Essentials，需同步修改 requirements.md 18.5 与 design.md 技术栈表
  - _需求: 18.4, 18.5_

- [x] 2. 建立工程骨架与包结构
  - **已完成并验证。** `pytest` 收集到 3 项分层守卫测试并全部通过；`import app, app.main, app.core, app.services, app.ui, app.config` 正常
  - 守卫测试比原计划多一条：除 core 层不碰 Qt 外，另加一条断言 `qfluentwidgets` 不泄漏出 `app/ui/theme`（原属任务 9，提前落地因为目录已存在），以及一条断言被扫描目录真实存在（防止包结构挪动后守卫静默变成空断言）
  - 按 `design.md`「目录结构」创建 `app/`、`app/core/`、`app/core/classifiers/`、`app/core/llm/`、`app/services/`、`app/ui/pages/`、`app/ui/widgets/`、`app/ui/theme/`、`app/config/`、`tests/unit/`、`tests/properties/`、`tests/fixtures/`、`resources/` 及各层 `__init__.py`
  - 创建 `pyproject.toml`：声明包名、入口、依赖名与下界（不写精确版本，精确锁定由 `requirements.txt` 承担），配置 pytest（`testpaths`、`markers = ["slow"]`）与 hypothesis profile（`ci` profile 提高到 200 次迭代）
  - 创建占位 `requirements.txt`，内容为待回填说明，由任务 13 填实
  - 创建 `app/main.py` 骨架：`QApplication` 装配、全局异常钩子、首次运行资源释放的调用点（具体释放逻辑在任务 7 实现）
  - 加一条架构守卫测试 `tests/unit/test_layering.py`：静态扫描 `app/core/**/*.py` 的 import，断言不出现 `PySide6`、`app.services`、`app.ui`、`app.config`
  - _需求: 18.7_

- [x] 3. 实现领域模型与统一编解码器
  - **已完成并验证。** `app/core/models.py` 落地，`tests/unit/test_models_codec.py` 25 项例子级测试全绿（全套 28 项，exit=0）
  - 解码由类型注解驱动（`get_type_hints` + `dataclasses.fields`），而非数据形态驱动：`"move"` 该变回 `ActionKind.MOVE` 还是留作字符串，只有目标类型知道
  - 以类目元组为 key 的字典编码成 `[[key, value], ...]` 键值对列表而非 JSON 对象。拼接成字符串 key 会让 `("财务/发票",)` 与 `("财务", "发票")` 撞车，override 就会挂到错误类目上；已有专门测试钉住这一点
  - `Category.make_id` 用 NUL 连接各段再哈希（NUL 不可能出现在 Windows 文件名里），同样是为了消除分隔符歧义
  - 集合编码时按编码结果排序，保证同一份数据两次写出的文件逐字节相同
  - 在 `core/models.py` 定义全部枚举：`ScanScope`、`Strategy`、`ActionKind`、`ConflictKind`、`ConflictPolicy`、`PrivacyLevel`、`RecordKind`
  - 定义数据类：`FileEntry`、`SubfolderInfo`、`ScanSelection`（含 `scope()` 与 `selected_closure()`）、`Category`、`PlanItem`、`PlanStats`、`SortPlan`、`JournalRecord`、`SourceSnapshotEntry`、`Manifest`、`RunMeta`、`ResultRow`、`ExecutionReport`、`ItemOverride`、`CategoryOverride`、`OverrideSet`
  - 类目路径统一用 `tuple[str, ...]` 表示，不用 `"财务/发票"` 字符串
  - 实现 `to_jsonable()` / `from_jsonable()` 一对函数，统一处理 `Path ↔ str`、`StrEnum ↔ str`、`tuple ↔ list` 的转换。后续六种序列化往返（`rules.yaml`、`journal.jsonl`、`manifest.json`、`settings.yaml`、LLM 缓存、`OverrideSet`）全部复用这一对函数，不允许各处重写
  - 实现 `ExecutionReport.to_csv_rows()` 为三组 `ResultRow` 的顺序拼接，使「CSV 行数 = 三组计数之和」成为恒等式
  - 定义注入 core 的窄选项对象：`ScanOptions`、`ClassifyOptions`、`ExecOptions`、`AIOptions`
  - _需求: 13.9, 13.10, 16.5, 18.3, 19.12_
  - _属性: 本任务不直接实现属性测试，但提供属性 9、15、23、28、29、30 共用的编解码器；六处往返的属性测试分别在任务 21、53、30、40、40、12 中实现_

- [x] 4. 实现 SafetyGuard
  - **已完成并验证。** `app/core/safety.py` + `tests/unit/test_safety.py`（26 项，1 项因环境不支持符号链接而 skip）
  - `AdmissionResult` 在 `__post_init__` 里保证「reject 必附带原因码、allow 必不附带」，使属性 1 的这条要求在构造期就成立，而不是靠调用方自觉
  - 系统目录集合改为优先读环境变量（`SystemRoot` / `ProgramFiles` / `ProgramData` 等），取不到才回落到需求 1.3 的字面值。硬编码 `C:` 会在 Windows 装在非 C: 盘的机器上漏掉真正的系统目录
  - **新增可注入的 `denied_segments`**（默认 `("appdata",)`）。这不只是为了方便测试：Windows 上 pytest 的 `tmp_path` 位于 `C:\Users\<用户>\AppData\Local\Temp` 之下，默认黑名单会把每个临时目录都判成系统路径，任何需要「一个可用根目录」的测试都建立不起立足点。任务 12 的属性 1 测试必须注入空黑名单，段匹配语义另用一个不会出现在临时路径里的段单独验证
  - `is_traversable` 同时判 `is_symlink()` 与 `is_junction()`。Windows 上 junction 不被 `is_symlink()` 识别，漏掉它意味着扫描可能顺着 junction 走出根目录
  - 在 `core/safety.py` 实现 `check_root(path)`，返回 `allow` 或 `reject` + 封闭原因码集合中的原因码
  - 盘符根判定返回 `drive_root`；`C:\Windows`、`C:\Program Files`、`C:\Program Files (x86)`、`C:\ProgramData` 及任一 `AppData` 目录内返回 `system_path`
  - `AppData` 判定按路径段相等比较，不用子串包含，避免误拒 `D:\MyAppDataBackup` 这类路径
  - 实现 `check_target(target, root)`，判定逻辑固定为 `target.resolve().is_relative_to(root.resolve())`，必须在 `resolve()` 之后比较
  - 实现符号链接与 junction 的判定辅助函数，供 Scanner 在 `scan.follow_symlinks` 为 false 时跳过
  - _需求: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_
  - _属性: 1_

- [x] 5. 实现 fsops 与进度节流器
  - **已完成并验证。** `app/core/fsops.py` + `app/core/progress.py`，配套 `tests/unit/test_fsops.py`（34 项）与 `tests/unit/test_progress.py`（19 项）
  - 函数名按 design.md 的契约命名：`atomic_move`、`copy_verify_trash`、`is_effectively_empty`（本清单初稿写的 `atomic_rename` / `copy_and_verify` / `is_dir_empty` 与设计不一致，已按设计为准）
  - `copy_verify_trash` 的三条失败分支都有专门测试：校验不一致 → 删目标保源标 failed；源不可读 → 不留残留目标；目标已校验通过但源未能进回收站 → **两份都保留**，因为删哪份都有丢数据风险
  - 校验失败时删目标用 `unlink` 而非回收站，那份副本是本次刚复制出的不完整数据、不是用户文件（需求 12.3）
  - `sanitize_segment` 额外处理了需求未提及但 Windows 会实际出问题的两类情况：结尾的点与空格（会被系统静默丢弃，导致实际名字与预期不符）、保留设备名（`CON`、`COM1` 等，含扩展名同样保留）
  - `next_available_name` 支持传入 `taken` 集合，避让同批次内尚未落盘但已被占用的目标名——只看磁盘会让同批两个文件算出同一个目标
  - `same_volume` 对尚不存在的目标路径回溯到最近的已存在祖先再判定，因为类目目录通常还没建出来
  - 在 `core/fsops.py` 实现：`same_volume(a, b)`（判定源可被 `VolumeStub` 替换）、`atomic_move(src, dst)`（`os.replace`）、`copy_verify_trash(src, dst)`（复制后比对 size 与 sha256）、`to_trash(path)`（`send2trash` 封装）、`is_effectively_empty(path)`（固定实现为 `next(os.scandir(path), None) is None`）、长路径与非法字符工具（259 字符判定、`\ / : * ? " < > |` 清洗、自动重命名 `文件 (2).pdf` 的生成）
  - 在 `core/progress.py` 实现 `ProgressThrottle`：纯 Python，不高于每 200ms 触发一次批量回调，时钟源可注入以便 `FakeClock` 替换
  - 实现 `CancelToken`：core 与外界交互只用入参、返回值、普通 callable，不得出现 Qt `Signal`
  - _需求: 9.7, 9.8, 12.1, 12.2, 20.11, 20.12_
  - _属性: 42（进度节流不丢计数）；另为属性 39（空目录判定唯一口径，M5 验证）提供 `is_dir_empty` 的唯一实现_

- [x] 6. 实现 Scanner 与 ScanSession
  - **已完成并验证。** `app/core/scanner.py`、`tests/fixtures/trees.py`、`tests/fixtures/models.py`、`tests/unit/test_scanner.py`（45 项）。全套 161 项，160 passed + 1 skipped，exit=0
  - **纠正了 design.md 关于 `deselect` 的一处规定。** 设计写「取消勾选 S 时把 S 的下级从 selection 中一并移除」，理由是「下级不可能在父级未选时仍参与整理」。但这与两处更高层约定冲突：需求 2.7 允许「只勾子不勾父」（design.md 自己的生成器维度表也把该组合列为必测），而属性 4 要求「先勾选 S 再取消勾选 S 之后勾选集合回到操作前状态」——若 deselect 顺带清掉下级，它就不是 select 的逆操作，该往返在「S 的下级原本已勾选」时必然失败。**已按需求与属性实现：deselect 只作用于目录自身。** 两条测试钉住：`test_selecting_child_without_parent_is_allowed`、`test_deselecting_parent_leaves_child_selected`
  - **「读过什么」与「勾选了什么」分开存储**：`_listing_cache` / `_children_cache` 只增不删，`_selection` 决定哪些桶参与 `entries()`。这是 select/deselect 能构成往返、且重复勾选不产生额外 I/O 的前提。新增 `read_count(dir)` 供属性 4 断言——只有次数能证明「不重新读取」，集合区分不了读过一次和读过三次
  - `_recursive_stats` 用字符串路径 + `os.scandir` 显式栈迭代，不构造 `Path`；`(st_dev, st_ino)` 访问集防止 hardlink 或 junction 导致同一目录被统计两遍
  - `follow_symlinks` 一个开关同时管文件与目录：为 false 时 `entry.is_file(follow_symlinks=False)` 与 `entry.is_dir(follow_symlinks=False)` 对符号链接都返回 False，链接自然既不进 entries 也不被递归，无需额外分支
  - **任务 5 加的 `CancelToken.__bool__` 护栏立刻抓到了本任务的一个真 bug**：我把参数默认值写成 `cancel = cancel or NullCancelToken()`，这会对 token 求布尔值。三条取消相关测试直接报 TypeError。已改为显式 `if cancel is None`，并在代码里留注释说明原因
  - `ScanDelta` 增加 `cancelled` 字段（设计没有）：补扫可能被取消，要能明确告知调用方「这次什么都没改」
  - 在 `core/scanner.py` 实现 `initial_scan(root, options)`：`os.scandir` 只收集根目录下的孤立文件为 `FileEntry`，`depth` 为 1；子文件夹自身及其内部内容不进 `FileEntry` 集合
  - 同时产出子文件夹清单：每项 `SubfolderInfo` 含孤立文件数、递归总文件数、递归总大小、`has_children`；统计过程只读元数据，不产出 `FileEntry`
  - 排除根目录下的 `.docsort` 目录及其内容（同时排除出子文件夹清单）；`scan.include_hidden` 为 false 时排除隐藏文件
  - 实现 `ScanSession` 持有 `ScanSelection` 与已收集条目：`select(S)` 只对 S 补扫并返回 `ScanDelta(added)`，S 的下级子文件夹保持未勾选；`deselect(S)` 移除来自 S 的条目；已扫描过的目录不重复读取
  - 单条目读取失败（权限、路径过长、I/O）时把原因写入该条目 `error` 并继续，不中断整体扫描
  - 取消检查点设在每处理 64 个目录项，命中即返回 `cancelled=True` 并丢弃结果，保证 500ms 内退出
  - 进度经 `ProgressThrottle` 200ms 批量回调
  - _需求: 2.2, 2.3, 2.4, 2.5, 2.7, 2.9, 2.10, 2.11, 2.13, 2.16, 2.17, 2.18, 2.19_
  - _属性: 3, 4, 5_

  ### 开工前补齐的规划（核对 design.md 后确定）

  **两处设计内部不一致，按以下方式统一：**

  1. **`ScanOptions` 的形状**。design.md「Scanner 与 ScanSession」一节给的是
     `include_hidden` / `follow_symlinks` / `excluded_names: frozenset[str]`；而
     `models.py`（任务 3 已落地）写的是 `scope` / `include_hidden` /
     `follow_symlinks` / `excluded_globs`。统一为设计版本，并做两处修正：
     - **去掉 `scope` 字段**。扫描范围是 `ScanSelection.scope()` 的派生值，放进
       选项对象会出现两个事实来源，一旦不同步就会出现「选项说 top_level_only、
       勾选集合却非空」这种自相矛盾的状态。
     - **`excluded_globs` 改为 `excluded_names: frozenset[str]`**，默认
       `frozenset({".docsort"})`。glob 排除在 requirements.md 里没有任何验收标准
       支撑，是本清单任务 11 描述里我自己加的；一并从任务 11 移除，需要的话另提
       需求再做。
  2. **进度回调的形状**。design.md 在 `ScanSession.initial_scan` 用
     `Callable[[int, str], None]`，在 `Executor.run` 用
     `Callable[[ProgressSnapshot], None]`。统一为后者（任务 5 已按此实现
     `ProgressThrottle`）：一种形状让 services 层只需一个适配器，属性 42 也才能
     对扫描与执行一致地成立。

  **六处未定的实现细节，按以下方式确定：**

  3. **`ScanError` 模型**（设计引用了但未定义）：定义在 `core/scanner.py`，字段为
     `path: Path`、`stage: str`（`entry` / `stat` / `listdir`）、`message: str`。
     `ScanResult.errors` 与 `FileEntry.error` 双写——前者供 UI 汇总展示，后者让
     单个条目自带失败原因（属性 5 要求「失败条目的 error 字段非空」）。
  4. **`is_hidden` 判定**：`entry.stat(follow_symlinks=False).st_file_attributes` 的
     `FILE_ATTRIBUTE_HIDDEN` 位，**或**文件名以 `.` 开头。两者取或：Windows 用属性位，
     但从 Unix 拷来的 dotfile 不带该属性位，只判属性位会漏。`st_file_attributes`
     仅 Windows 存在，取值用 `getattr` 兜底以便非 Windows 环境仍可跑测试。
  5. **`depth` 规则**：相对根目录的层级数。根目录下的孤立文件为 1，被勾选的直接
     子文件夹 S 中的孤立文件为 2，依此类推。
  6. **`select` 被取消时**：丢弃本次补扫的部分结果、勾选集合不变，`ScanDelta`
     增加 `cancelled: bool` 字段（设计的 `ScanDelta` 没有该字段，此处补上）。
     理由与 `initial_scan` 一致——半个范围的方案比没有方案更危险。
  7. **`elapsed_ms` 的时钟可注入**，默认 `time.monotonic`，与 `ProgressThrottle`
     共用 `FakeClock` 替身。
  8. **`.docsort` 按名字在任意层级排除**，不限于根目录直属。需求 2.19 只写了根
     目录，但那是我们自己的元数据目录，任意层级都不该被当成用户文件整理；镜像
     副本就写在根目录的 `.docsort\` 下（需求 13.5）。

  **实现顺序**：`ScanError` / `ScanResult` / `ScanDelta` 模型 → 单目录列举
  （孤立文件 + 直属子目录，含隐藏与排除过滤）→ 递归统计（含 `(dev, ino)` 访问集
  防重复计数）→ `initial_scan` → `expand` → `select` / `deselect` 增量维护 →
  取消与进度接线。

  **测试计划**：`tests/fixtures/trees.py` 先落地目录树构造器（任务 12 的生成器要
  复用它），覆盖设计「共用的生成器与替身」表里的目录形状维度；`tests/unit/
  test_scanner.py` 覆盖——只收孤立文件、子目录内容不进 entries、`.docsort` 双重
  排除、隐藏文件开关、统计值与朴素 `os.walk` 一致、勾选/取消勾选往返、下级不继承
  勾选、已扫描目录不重复读取、单条目失败不中断、取消后丢弃结果。属性 3/4/5 的
  Hypothesis 版本仍留在任务 12。

- [x] 7. 实现 Settings 与 SettingsManager
  - **已完成并验证。** `app/config/settings.py` + `tests/unit/test_settings.py`（48 项）
  - `SettingsManager.PATH` 改为 `default_app_dir()` 函数。设计写的是类属性 `Path(os.environ["APPDATA"]) / ...`，那会在 `APPDATA` 缺失时于 **import 期**抛 KeyError，连报错界面都弹不出来；改成函数 + 回落到 `~/.docsorter`。`base_dir` 与 `keyring_backend` 都可注入，测试不往真实 `%APPDATA%` 写文件、不依赖机器上有可用凭据管理器
  - `save()` 用共用的 `to_jsonable` 而非设计写的 `asdict`：`asdict` 会把枚举与 `Path` 原样留下，`yaml.safe_dump` 无法序列化；用 `to_jsonable` 则与 journal / manifest 走同一套转换规则（任务 3 的目的所在）。落盘经临时文件 + `os.replace` 原子替换
  - `load()` 的容错复用 `from_jsonable` 做 happy path、失败才回落字段默认值，使「合法配置往返」与「损坏配置容错」共用同一份类型知识
  - **需求给出的上下界在加载时夹紧**：`min_confidence` 0..1、taxonomy 类目数 ≤12（需求 7.6）、层级 ≤2（需求 7.6）、批大小 80..120（需求 7.7）、抽样 ≤300（需求 7.5）、保留条数与天数 ≥1。越界不再等到调用 LLM 时才暴露
  - 新增 `UISettings.dry_run_completed_roots`：需求 11.2 要求首次对某目录执行前强制模拟运行一次，「是否首次」必须跨会话持久化，否则重启即可绕过这道防护
  - `to_scan_options` 刻意不传 `scope`。`ScanSettings.scope` 是持久化值（需求 18.8，用于恢复界面状态），运行期真实范围由 `ScanSelection.scope()` 派生（需求 2.9）——两者分工明确，避免第二个事实来源
  - _属性: 10（配置文件破坏容错）、30（配置序列化往返一致性）_
  - 在 `config/settings.py` 定义 `Settings` dataclass 树，覆盖 `scan.scope`、`scan.include_hidden`、`scan.follow_symlinks`、`classify.min_confidence`、`classify.merge_small_categories`、`classify.small_category_threshold`、`date.granularity`、`ai.*`、`cleanup.remove_empty_dirs`、冲突策略、默认动作、历史保留条数与天数
  - 默认值按需求设定：`scan.scope = top_level_only`、`ai.enabled = false`、`cleanup.remove_empty_dirs = false`、默认策略 `by_type`、默认动作 `移动`、默认冲突策略 `auto_rename`、`min_confidence = 0.6`、`small_category_threshold = 3`、`ai.timeout_seconds = 30`、保留 20 条 / 30 天
  - 持久化到 `%APPDATA%\DocSorter\settings.yaml`，复用 `to_jsonable()` / `from_jsonable()`
  - 文件缺失或字段非法时用默认值填充并重写文件，返回合法配置对象，不抛未捕获异常
  - 实现首次运行把随包的 `rules_default.yaml` 释放到 `%APPDATA%\DocSorter\rules.yaml`
  - 实现从 `Settings` 构造 core 窄选项对象的转换函数，保持 config → core 单向依赖
  - _需求: 2.1, 3.2, 4.1, 6.1, 6.8, 11.1, 15.3, 15.5, 18.1, 18.2, 18.3, 18.8, 20.1_
  - _属性: 10（配置文件破坏容错）、30（配置序列化往返一致性）_

- [x] 8. 实现 services 基类与 ScanService
  - **已完成并验证。** `app/services/base.py`、`app/services/scan_service.py` + `tests/unit/test_scan_service.py`（22 项）。全套 225 项，224 passed + 1 skipped，exit=0
  - `conftest.py` 设 `QT_QPA_PLATFORM=offscreen`：Qt 测试不依赖桌面会话，跑测试时也不会闪出窗口
  - `WorkerService._launch(job)` 接受 `(CancelToken, ProgressSink) -> Any` 的普通 callable，线程与取消只写一遍，五个子类各自只组装 job 与解释结果
  - `_Worker` 刻意不持有 service 引用——跨线程访问 service 状态是竞态的温床，所有结果都经信号回主线程
  - **已有作业运行时 `_launch` 拒绝而非排队**：`ScanSession` 的 `_listing_cache` 与 `_selection` 不是线程安全的，并发跑两个作业会互相踩。拒绝就维持了单线程访问的假设，因此 ScanService 不需要额外加锁
  - 子类钩子 `_transform` 在 `finished` 之前执行，保证 `subfoldersReady` 先于 `finished` 发出——UI 拿到 finished 时子文件夹清单已就位，有测试钉住这个顺序
  - `check_root` 与 `expand` 同步执行不开线程：前者是纯路径运算，后者只读单个目录且树形控件的 `expanded` 信号里就要拿到子节点
  - 准入拦截放在启动作业**之前**，用户不会先看到「扫描中」再被拒
  - 写测试时发现一处：原本想测「根目录不可读时扫描仍完成」，但全局打补丁 `os.scandir` 会连 `SafetyGuard._is_readable` 一起影响，根目录不可读在准入阶段就被判 `NO_PERMISSION` 拒掉。这是正确行为，已拆成两条测试——子目录读取失败走 `ScanResult.errors` 隔离，根目录不可读走准入拒绝
  - 在 `services/base.py` 实现 `WorkerService` 基类：`QThread` 生命周期管理、`CancelToken` 桥接、把 core 的 callable 回调翻译成 Qt 信号
  - 在 `services/scan_service.py` 实现 `ScanService`：包装 `ScanSession` 的初次扫描、补扫、取消，暴露 `progress` / `finished` / `failed` 信号
  - services 是唯一同时看见 Qt 与 core 的层，不在此层放业务判定
  - _需求: 2.12, 2.13, 2.18_

- [x] 9. 建立 UI 主题层
  - **已完成并验证。** `app/ui/theme/{tokens,components,qss}.py`，令牌合规测试 13 项
  - `tokens.py` 零 Qt 依赖，属性 41 可直接断言取值而不必拉起窗口
  - `components.py` 是唯一 import `qfluentwidgets` 的模块，且**组件库不可用时自动回落到等价原生 Qt 控件**（`FLUENT_AVAILABLE` 标志）。一个能用但样式朴素的窗口，比一个打不开的窗口有用；这也让任务 58 若决定换库时有现成的降级基线
  - 确认对话框收敛成单个 `confirm()` 入口：需求 11.3、19.10、20.3、20.4 都要求二次确认，集中一处才能保证文案与行为一致
  - 样式表里的间距数值全部从 tokens 取，测试用正则扫 `padding`/`margin` 断言落在 {4,8,12,16,24} 内薄封装
  - 在 `app/ui/theme/tokens.py` 定义设计令牌：主色 `#2563EB`、圆角 8px、间距栅格 4/8/12/16/24、状态色 成功 `#16A34A` / 冲突 `#F59E0B` / 失败 `#DC2626`、字体族 Segoe UI + 微软雅黑 UI、类目 chip 10 色柔和色板
  - 在 `app/ui/theme/` 下建立组件再导出层：**所有对 `qfluentwidgets` 的 import 只允许出现在这一层**，页面与控件统一从主题层取组件与令牌
  - 实现主题跟随系统深浅色
  - 加一条守卫测试：断言 `app/ui/pages/` 与 `app/ui/widgets/` 下不出现直接 import `qfluentwidgets`
  - 收敛 import 的目的是控制组件库授权风险的改动面（见任务 58）：若后续因 GPLv3 授权需要更换组件库，改动集中在主题层
  - _需求: 17.3, 17.4, 17.5, 17.6, 17.7, 17.8_
  - _属性: 41_

- [x] 10. 实现主窗口与侧边导航骨架
  - **已完成并验证。** `app/ui/main_window.py`，四个导航入口齐备；真实平台插件下启动自检通过（非 offscreen）：`qfluentwidgets available: True`、标题正确、1180x760、`visible: True`、`exec returned: 0`
  - 导航图标用 `_icon("BROOM", "FOLDER", "HOME")` 这样的候选列表 + `getattr` 兜底。组件库不同版本的图标枚举有增删，写死名字会让「换个版本就启动不了」
  - `SortFlowPage` 是编排层：把页面信号接到 ScanService、把 service 信号接回页面，不含业务判定
  - 未实现的三页（历史记录 / 规则管理 / 设置）用占位页并**写明在哪个里程碑落地**，避免看起来像坏了
  - `closeEvent` 先取消并等待后台作业再落盘配置，防止关窗时线程仍在写
  - 在 `app/ui/main_window.py` 基于主题层的 `FluentWindow` 实现侧边导航，四个入口：整理流程、历史记录、规则管理、设置
  - 整理流程内以选择目录、扫描分析、方案预览、执行结果四个步骤组织
  - 各页面先建空壳，由后续里程碑填充
  - _需求: 17.1, 17.2_

- [x] 11. 实现选择目录页与子文件夹选择器
  - **已完成并验证。** `app/ui/pages/{select_page,scan_page}.py`、`app/ui/widgets/{states,subfolder_picker}.py`，含一条选目录→扫描→勾选→取消勾选的端到端测试
  - **子文件夹选择器刻意不用 Qt 三态复选框。** 三态的视觉语义是「部分子项被选中」，会让用户以为勾父就等于勾了一批子；而真实语义是「只作用于该文件夹的散落文件」（需求 2.7 勾选不继承）。每个节点勾选状态彼此独立，视觉上也就该彼此独立
  - 列表分「散落文件」与「全部文件」两列并各带 tooltip 说明：前者是勾选后真正参与整理的数量，后者仅供参考。只显示一个数字会让用户误判影响面
  - 首次勾选弹一次后果说明（需求 2.8），取消则复选框弹回
  - 子节点用占位项 + `itemExpanded` 懒加载，展开才读盘
  - 扫描页三种状态用 `QStackedWidget` 互斥切换（骨架屏 / 结果 / 错误 / 空态），而非显隐控制——避免出现两种状态同时可见的中间态
  - 骨架屏用不确定进度条而非假灰条：扫描初期总量未知，假灰条会暗示「马上就好」
  - 补扫被拒绝或被取消时把复选框弹回原状，保证界面与 `ScanSession` 的实际状态一致
  - 选择目录页：拖拽与浏览两种输入方式、最近目录列表、高级选项（隐藏文件、是否跟随符号链接）
  - 高级选项**不含** glob 排除：该功能在 requirements.md 中无验收标准支撑，已确认不做（详见任务 6 的规划说明）
  - 提交目录时调用 `SafetyGuard.check_root`，`reject` 时按原因码给出具体提示；`allow` 时显示路径并启用「扫描」动作
  - 在 `app/ui/widgets/subfolder_picker.py` 实现子文件夹选择器：树形逐级展开，每一级每个子文件夹独立勾选控件，展示孤立文件数 / 递归总文件数 / 递归总大小
  - 勾选时在勾选处显示后果说明，明示「该文件夹下的孤立文件将被移出、提升到根目录下的类目目录」；下级子文件夹不继承勾选状态
  - 勾选任一子文件夹时把 `scan.scope` 置为 `selected_subfolders`
  - _需求: 1.8, 2.6, 2.7, 2.8, 2.9_

- [ ] 12. 建立测试基础设施并实现 M1 属性测试
  - 在 `tests/fixtures/` 实现目录树生成器 `file_trees`（递归结构、可控深度宽度、物化到 `tmp_path`），覆盖 `design.md`「共用的生成器与替身」表中的全部维度：文件名（中文/空格/点开头/超长/含非法字符）、目录形状（空目录/只含子目录/仅含 `desktop.ini` 与 `Thumbs.db`/`.docsort`/深层嵌套）、勾选组合（空集/单个/父子同勾/只勾子/全选）
  - 实现替身：`FakeClock`、`VolumeStub`、`TrashRecorder`、`MemoryKeyring`（`FakeProvider` 留到 M5）
  - 在 `tests/fixtures/models.py` 实现参照实现 `naive_scan(root, scope)`，用 `os.walk` 直接算出应有 entries 与子文件夹统计
  - 实现属性测试：属性 1（准入判定完备确定）、属性 3（扫描输出与朴素模型一致）、属性 4（勾选取消往返、补扫只覆盖新增）、属性 5（扫描错误隔离，正文提取维度留到 M5）、属性 30（进度节流）、属性 40 与 42（模型编解码与配置往返）
  - 涉及真实文件 I/O 的属性用 `max_examples=50` 且 `deadline=None`
  - _需求: 1.1, 1.2, 1.3, 2.2-2.5, 2.7, 2.9-2.11, 2.13, 2.16, 2.17, 2.19, 18.3_
  - _属性: 1, 3, 4, 5, 30, 40, 42_

- [x] 13. 【需用户手动执行结果的回填】锁定依赖版本
  - **已完成。** `requirements.txt` 按实际 `pip list --format=freeze` 回填，15 个直接依赖 + 32 个传递依赖全部 `==` 锁定，分「运行期 / 开发期 / 传递」三组；未锁版本的来源保留在 `requirements.in`
  - 依据任务 1 反馈的 `pip list --format=freeze` 输出，把全部运行期与开发期依赖以 `==` 精确写入 `requirements.txt`
  - 在文件头部注释记录锁定时点的 Python 版本（3.12.13）
  - `pyproject.toml` 只保留依赖名与下界，不与 `requirements.txt` 重复承担锁定职责
  - 首次安装的依赖解析已完成，以下是 pip 解析出的目标版本，可作为回填的预期值核对（**安装未成功，尚未落地，须以实际 `pip list` 为准**）：
    - Qt 相关：`PySide6 6.11.1`、`shiboken6 6.11.1`、`PySide6-Essentials 6.11.1`、`PySide6-Addons 6.11.1`
    - UI：`PySide6-Fluent-Widgets 1.11.2`、`PySideSix-Frameless-Window 0.8.1`、`darkdetect 0.8.0`
    - 运行期：`pyyaml 6.0.3`、`send2trash 2.1.0`、`chardet 7.4.3`、`keyring 25.7.0`、`httpx 0.28.1`、`pypdf 6.14.2`、`python-docx 1.2.0`、`openpyxl 3.1.5`、`python-pptx 1.0.2`
    - 传递依赖：`lxml 6.1.1`、`Pillow 12.3.0`、`XlsxWriter 3.2.9`、`pywin32 312`、`pywin32-ctypes 0.2.3`、`et-xmlfile 2.0.0`、`typing_extensions 4.16.0`、`anyio 4.14.2`、`certifi 2026.7.22`、`httpcore 1.0.9`、`h11 0.16.0`、`idna 3.18`、`jaraco.classes 3.4.0`、`jaraco.functools 4.6.0`、`jaraco.context 6.1.2`、`more-itertools 11.1.0`
    - 开发期：`pytest 9.1.1`、`pytest-qt 4.5.0`、`pytest-benchmark 5.2.3`（`hypothesis` 与 `pyinstaller` 未解析到即中断）
  - _需求: 18.5_

---

## M2 规则与规划

交付：`RuleSerializer` / `RuleEngine`、分类器与管线、`Planner`、`TargetAllocator`、冲突预检。
本阶段应通过属性：2、7、8、9、10、16、17。

- [x] 14. 实现规则引擎与规则序列化
  - **已完成并验证。** `app/core/rules.py` + `app/config/rules_default.yaml` + `tests/unit/test_rules.py`（52 项）。**同时消掉了启动时的 `内置规则文件缺失` warning**
  - 解析用 `yaml.compose()` 而非 `safe_load`：只有保留 `start_mark` 的节点树才能给出真实行号（需求 4.3）。用户改坏自己的规则文件时，「第 17 行 patterns 字段不是列表」比「配置无效」有用得多
  - **校验失败整体回落，不做部分采纳**：半套规则会让一半文件按新规则走、一半按旧规则走，产出用户看不懂的结果
  - 扩展名 pattern 归一化成小写含点，容忍用户写 `pdf` 或 `.PDF`
  - `match_text` 刻意只用关键词规则、不用正则：截图那条 `^screenshot` 是为文件名设计的，套到正文上会得到莫名其妙的命中
  - 加了一条测试断言 `rules_default.yaml` 与 `builtin_rules()` 等价，防止两处漂移
  - 在 `core/rules.py` 实现 `RuleSerializer`：把 `rules.yaml` 解析为规则对象集合（每条含 id、type、priority、patterns、category、enabled），并实现反向序列化
  - 解析失败（YAML 语法错误、缺必填字段、类型错误、未知键）返回含行号与字段名的结构化错误，不抛未捕获异常；此时 `RuleEngine` 回落到内置默认规则完成分类
  - 实现 `RuleEngine`：关键词规则 priority 200，扩展名规则 priority 100，文件名关键词忽略大小写匹配
  - 编写 `app/config/rules_default.yaml`，完整覆盖需求 4.6 的 6 组关键词类目（财务/发票、财务/报销、合同协议、简历、证件资料、截图）与需求 4.7 的 11 组扩展名类目（文档/PDF、文档/Word、文档/表格、文档/演示、文档/文本、电子书、图片、音视频、压缩包、安装程序、代码）
  - 序列化往返复用 `to_jsonable()` / `from_jsonable()`
  - _需求: 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.9, 3.9_
  - _属性: 8, 10_

- [x] 15. 实现 Classifier 协议与管线
  - **已完成并验证。** `app/core/classifiers/base.py`
  - `ClassifyContext` 显式传入全部外部信息（配置、LLM 快照）。分类器一旦能自己读配置或发请求，确定性（需求 3.13）就无法从结构上保证
  - **`by_date` 刻意不进 priority 竞争序列**：`BY_DATE` 下它是唯一来源，`TYPE_AND_DATE` 下它作为第二级由 Planner 拼接（需求 3.5）。混进竞争序列会让「类型+时间」变成「类型或时间」
  - `build_pipeline` 跳过缺失的分类器而不报错：`SMART` 策略下 content 与 llm 可能因「AI 未启用」缺席，此时管线自然退化为 filename → extension，方案仍覆盖全部文件（需求 6.4、6.6）
  - 全部落空时的兜底 reason 会说明「最高置信度 X 低于阈值 Y」，而不是只说「没命中」
  - 在 `core/classifiers/base.py` 定义 `Classifier` 协议、`Suggestion`（类目、confidence、reason）、`ClassifierPipeline`、四种策略的装配表
  - 管线按 priority 由高到低求值，采纳第一个 `confidence >= min_confidence` 的建议；全部低于阈值则归入 `_未分类`
  - 每条结果必须附带 reason 字符串与 0 到 1 之间的 confidence
  - 支持按 priority 注册新分类器，注册后求值次序与 priority 一致
  - 相同配置下重复求值产出相同类目分配（确定性）
  - _需求: 3.1, 3.3, 3.6, 3.7, 3.8, 3.10, 3.11, 3.13_
  - _属性: 7, 9_

- [x] 16. 实现三个基础分类器
  - **已完成并验证。** `by_extension.py`、`by_date.py`、`by_filename.py`
  - 关键词命中置信度 0.9 高于正则 0.85：关键词是用户自己写下的词，「发票」出现在文件名里几乎不是巧合；正则更容易误伤（`image_1.png` 未必是截图）
  - `by_date` 的时间类目第二级带年份前缀（`2024/2024-03` 而非 `2024/03`），把目录单独拷出去也不会只剩一个 `03` 让人猜
  - `by_date` 不读时钟、只依赖 `mtime`，且对荒谬取值（负数、超范围）回落到纪元而不抛异常打断整批分类
  - **色板从 `ui/theme/tokens.py` 移到 `core/models.py`，由 tokens 再导出。** `Category.color` 是 core 字段且要随 manifest 落盘，而 core 不能 import ui（需求 18.7）；两边各存一份就会出现「界面颜色与 manifest 记录不一致」
  - `by_extension.py`：按扩展名规则表分类
  - `by_date.py`：按 `FileEntry.mtime` 产出时间类目，粒度由 `date.granularity`（`年` / `年-月`）决定
  - `by_filename.py`：关键词与正则匹配，priority 高于扩展名
  - 装配四种策略：`按文件类型` = filename → extension；`按时间` = date 单级；`类型+时间` = 类型一级 + 时间二级；`智能` = filename → content → llm → extension（content 与 llm 在 M5 接入，此阶段先留空实现）
  - _需求: 3.3, 3.4, 3.5, 3.6, 3.9_
  - _属性: 7, 9_

- [x] 17. 实现五态冲突判定
  - **已完成并验证。** `app/core/conflicts.py`
  - 判定顺序 `path_escape` → `path_too_long` → `locked` → `exists` → `none`。前三态与冲突策略**无关**：它们表示「目标路径根本不可用」，改名或跳过都救不了。顺序写反会让一个既超长又同名的目标被当普通重名去改名，改完还是超长
  - `TargetAllocator._taken` 跟踪本批已占用的目标。**这一层是必需的**：同批两个源文件可能算出同一目标，而它们都还没落盘、磁盘上都不存在，只查磁盘会让执行时互相覆盖。CLI 实测到了这个场景（见任务 19）
  - 自动改名后若反而超长，如实返回 `path_too_long` 而不假装解决了
  - `locked` 用 `os.open(path, os.O_RDWR)` 试探后立刻关闭：只看只读属性位测不出「被别的进程独占」
  - 在 `core/conflicts.py` 实现冲突判定，取值域固定为 `none` / `exists` / `path_too_long` / `locked` / `path_escape`，且判定顺序确定、结果对每个 `PlanItem` 唯一
  - `exists`：目标已存在同名文件。`auto_rename` 策略生成 `文件 (2).pdf` 并记 `renamed_from`；`skip` 策略把 action 置为 `skip`；`overwrite` 策略由 UI 二次确认后才允许执行
  - `path_too_long`：目标绝对路径超过 259 字符
  - `locked`：源文件无法以写入方式打开（被占用或只读）
  - `path_escape`：`SafetyGuard.check_target` 判定失败，是该冲突态的唯一来源
  - _需求: 9.3, 9.4, 9.5, 9.6, 9.7, 9.9_
  - _属性: 16_

- [x] 18. 实现 Planner 与 TargetAllocator
  - **已完成并验证。** `app/core/planner.py` + `tests/unit/test_planner.py`（56 项）。全套 344 项，343 passed + 1 skipped，exit=0
  - **小类目合并在 override 叠加之前**（需求 3.12 与 19 的先后）：反过来会把用户手工建的小类目自动合并掉，而用户意图优先级更高
  - `_未分类` 不参与小类目合并：把它并进「其他」只会让用户更难找到那些没被识别的文件
  - 条目按路径排序后再构造，使自动重命名的编号不随遍历顺序漂移——属性 17 的幂等性依赖这一点
  - 跨卷空间校验按目标卷分组累加后**整体阻止执行**（需求 9.10）：执行到一半才发现空间不足，留下的是半整理状态
  - `EmptyDirPredictor` 与 Executor 的实际清理共用 `fsops.is_predicted_empty`，预测清单与实际删除不可能偏差（属性 38）
  - `Planner.build` 留了 override 叠加的接入点，M3 的 `OverrideLayer` 在第 3 步接入
  - 在 `core/planner.py` 实现 `Planner`：调用管线产出基础方案，组装 `SortPlan`（root、strategy、categories、items、unclassified、scope、selected_subfolders）
  - `TargetAllocator` 构造目标路径，形态固定为 `根目录\清洗后类目路径\文件名`，类目顶层段的父目录恰为根目录；类目段清洗 `\ / : * ? " < > |` 为下划线
  - 类目目录创建为根目录的直接子文件夹，使其在 `top_level_only` 下天然落在扫描范围之外
  - 兜底：`FileEntry` 当前父目录已等于目标类目目录时把 action 置为 `skip`
  - 小类目合并：`merge_small_categories` 为 true 且类目文件数小于阈值时改归入 `其他`
  - 跨卷时校验目标卷剩余空间不小于待复制总大小的 1.1 倍，不足则阻止执行并给出提示
  - 实现 `SortPlan.stats()` 供统计卡使用
  - _需求: 3.12, 9.1, 9.2, 9.8, 9.10, 9.11, 9.12, 9.13_
  - _属性: 2, 17_

- [x] 19. 实现无界面 CLI 入口
  - **已完成并在真实目录上验证。** `app/core/cli.py`
  - 实测命令与结果（`--root app --select core --strategy type_and_date`）：扫到 2 个根目录散落文件 + 5 个子文件夹带统计 → 勾选 `core` 新增 10 个文件 → 总数 12 → 产出两级类目 `代码/2026-07` → 每条都带命中理由
  - **CLI 实测抓到了同批目标撞车这个最微妙的场景**：`app/__init__.py` 与 `app/core/__init__.py` 都算出目标 `代码/2026-07/__init__.py`，第二个被自动改名为 `__init__ (2).py` 并标 `exists`。两者在目标位置都还不存在，只查磁盘的实现会静默让一个覆盖另一个
  - CLI 默认 `check_locked=False`：逐个文件试 `O_RDWR` 在大目录上很慢，而 CLI 只用于查看方案、不执行
  - 在 `core/cli.py` 实现 `python -m app.core.cli --root <目录> --strategy <策略> --dry-run`，在无 Qt 环境下跑通「准入 → 扫描 → 分类 → 规划 → 冲突预检」整条链并打印方案摘要
  - 这条入口是 core 层边界是否守住的可执行证据，也是手工调试的主要手段
  - _需求: 18.7_

- [x] 20. 实现 PlanService
  - **已完成。** `app/services/plan_service.py`
  - 会话态的 `OverrideSet` 由本类**独家持有**：override 是规划链的独立输入，散落到各页面各存一份的话，「切换策略后手工调整还在不在」就取决于哪个页面先刷新（需求 19.3）
  - 需求 19.3 列出的六个重算触发场景全部走 `rebuild()` 这一个入口，保证行为一致
  - `set_engine()` 供规则库修改后换引擎，下一次重算生效（需求 4.8）
  - 在 `services/plan_service.py` 包装 `Planner`，暴露方案生成与重算的信号；持有会话内的 `OverrideSet`（M3 接入）
  - _需求: 9.1_

- [ ] 21. 实现 M2 属性测试
  - 属性 2（目标路径永不逃逸且形态统一）、属性 7 与 9（分类管线阈值、兜底、确定性）、属性 8（规则匹配与优先级）、属性 10（规则与配置文件破坏容错）、属性 16（冲突五态完备性）、属性 17（方案结构与幂等 skip）
  - 生成器需覆盖配置组合维度：四种策略 × 三种冲突策略 × `include_hidden` 真假
  - _需求: 3.7, 3.8, 3.13, 4.3, 4.5, 4.9, 9.2-9.13, 18.2_
  - _属性: 2, 7, 8, 9, 10, 16, 17_

---

## M3 预览与 override

交付：方案预览页三栏与全部编辑交互、`PlanTreeModel` 懒加载、`OverrideLayer`。
本阶段应通过属性：18、19、20、21、22、23、41。

- [x] 22. 实现 OverrideLayer
  - **已完成并验证。** `app/core/overrides.py` + `tests/unit/test_overrides.py`（30 项）
  - **与设计签名的差异（已在模块文档里说明）**：design.md 写 `apply(base: SortPlan, overrides) -> (SortPlan, ApplyReport)`，但它自己的 Planner 五步流水线又把 override 叠加放在第 3 步、target 构造**之前**。两者不能同时成立——拿到 SortPlan 时 target 已算好，改类目就得把第 4、5 步再跑一遍。按流水线顺序实现：本层只解析「每个文件最终归哪个类目、勾选与否」，target 与冲突仍由 Planner 统一做一次
  - 重映射支持链式（A 改名成 B、B 并进 C，则 A 的成员落到 C），并用访问集 + 跳数上限防 A→B→A 把调用方挂死
  - `merged_into` 优先于 `renamed_to`：合并是更强的意图
  - 类目重建（需求 19.6）放在条目覆盖**之后**判断：此时最终类目集合已确定，比在第 2 步猜更准
  - 条目 override 记的可能是改名前的类目名，因此要先过一遍重映射再落地
  - `resolve` 是纯函数，遍历 override 时按 key 排序，不依赖字典插入顺序
  - 在 `core/overrides.py` 定义 `ItemOverride`、`CategoryOverride`、`OverrideSet` 的操作接口，条目级 override 以文件绝对路径为 key
  - 实现 `OverrideLayer.apply(base_plan, overrides)`：override 叠加到基础方案之上，优先级高于任何分类器结果
  - override 引用的类目在新基础方案中不存在时，按原有名称与颜色重建该类目
  - override 引用的文件在新扫描结果中不存在时丢弃该条并返回被丢弃条数
  - `OverrideSet` 的 JSON 序列化复用 `to_jsonable()` / `from_jsonable()`，类目元组 key 序列化为段列表
  - 在 `tests/fixtures/models.py` 实现参照实现 `naive_apply_overrides(base, overrides)`
  - _需求: 19.1, 19.2, 19.4, 19.5, 19.6, 19.7, 19.12_
  - _属性: 20, 22_

- [x] 23. 把 override 接入 Planner 重算链
  - **已完成并验证。** `Planner.build` 第 3 步调用 `OverrideLayer().resolve`，`PlanResult` 带上 `ApplyReport`
  - 三条端到端测试钉住需求 19.3 的核心场景：**换规则库**、**换策略**、**连续两次重算**，手工调整都还在
  - `PlanService` 在收到结果后把失效的 override 从集合里清掉，否则它们会在每次重算时反复出现在「已丢弃」提示里
  - `OverrideSet` 补上 `version` 字段以对齐设计
  - `Planner` 在重算末段调用 `OverrideLayer.apply()`，override 作为规划链的独立输入而非对 `SortPlan` 的原地修改
  - 六个重算触发场景全部保留 override：切换 AI 开关、切换分类策略、修改规则库、重新扫描、改变扫描范围、切换冲突策略
  - 相同配置与扫描范围下连续两次重算产出相同类目分配与相同 included 状态
  - _需求: 19.3, 19.4, 19.5, 19.13, 19.14, 6.7, 2.10_
  - _属性: 20, 21, 22, 23_

- [x] 24. 实现 PlanTreeModel
  - **已完成并验证。** `app/ui/widgets/plan_tree_model.py` + `app/ui/widgets/plan_filter.py`
  - 节点是 `slots` dataclass 且只存 `SortPlan.items` 的下标，实际数据不复制——这是把 10 万行内存开销压下来的关键
  - `canFetchMore` / `fetchMore` 每次追加 200 行，测试验证了「未 fetch 前类目下是 0 行但 `hasChildren` 为真」以及分批追加的边界
  - 整理前 / 整理后只换分组键（`entry.path.parent` vs `category_id`），不重建 `items`（需求 10.4）
  - 筛选走 `QSortFilterProxyModel` 的行白名单，不复制数据。**未加载的类目行一律先放行**——否则用户会以为筛选把整个类目排除了
  - 筛选刷新用公开槽 `invalidate()`：这个 PySide6 版本把 `invalidateFilter` 和 `invalidateRowsFilter` 都标了废弃
  - 在 `app/ui/widgets/plan_tree_model.py` 实现自定义 `QAbstractItemModel`，配合 `QTreeView` 承载预览树并按需加载子节点
  - 不使用 `QTreeWidget` 全量填充：10 万条目下 item 对象开销与构建时间无法满足 100ms 响应
  - 支持 `整理前` / `整理后` 两种视图切换
  - _需求: 10.4, 10.13, 10.14_

- [x] 25. 实现预览页三栏骨架与统计卡
  - **已完成并验证。** `app/ui/pages/preview_page.py` + `tests/unit/test_preview_ui.py`（26 项）。全套 393 项，392 passed + 1 skipped，exit=0
  - 冲突数与未分类数非零时统计卡数字转橙色，不用展开树也能看到规模
  - 副标题明写「此刻磁盘上什么都没变」——这一页的职责是让用户敢确认，而不是让他猜
  - **空间不足时整体禁用「开始整理」**（需求 9.10），有专门测试
  - 没有方案时「开始整理」也是禁用的
  - 界面端到端自检（真实平台插件）：7 个测试文件 → 7 个类目（关键词命中发票/合同/简历/截图，`.xlsx` 走扩展名，`.zzz` 进未分类）→ 走到预览页、统计卡与类目列表都正确填充
  - 顶部统计卡显示文件总数、类目数、待移动数、冲突数、未分类数，数据取自 `SortPlan.stats()`
  - 三栏布局：左类目列表、中目标结构树、右文件详情
  - 顶部放 AI 开关（作用于 `ai.enabled`，M5 接入实际行为）
  - 显示「已保留 N 项手工调整」与被丢弃 override 条数提示
  - 提供子文件夹选择器入口，并显示当前扫描范围包含的子文件夹数量
  - _需求: 10.1, 10.16, 19.7, 19.8, 6.2_

- [x] 26. 实现类目列表编辑交互
  - **已完成。** `app/ui/widgets/category_list.py`
  - 改名 / 换色 / 合并 / 新建 / 删除**只发信号，不自己改方案**：所有修改必须经 `PlanService` 记入 override 再重算，否则「切换策略后手工调整还在不在」就取决于哪条路径先跑
  - 删除类目的确认框写明「该类目下的 N 个文件会退回 `_未分类`，不会被删除；这只是调整方案，磁盘上的文件此刻不受影响」——避免用户以为「删除类目」等于删文件
  - 「清除全部手工调整」有二次确认并明说不可撤销（需求 19.9、19.10）
  - 左栏类目列表支持改名、换色、合并、新建、删除；类目 chip 从 10 色柔和色板轮转取色
  - 删除类目时把该类目下的文件改归入 `_未分类`
  - 每个编辑动作记入 override 集合
  - 提供「清除全部手工调整」动作并要求二次确认，确认后清空 override 并重算
  - _需求: 10.2, 10.3, 10.15, 17.8, 19.1, 19.9, 19.10_
  - _属性: 18, 19_

- [x] 27. 实现目标结构树交互（部分）
  - **已完成**：冲突橙色角标（需求 10.5）、`旧名 → 新名`（需求 10.6）、复选框批量排除（需求 10.10）、AI 角标位（需求 8.7）、tooltip 含原路径与目标路径
  - 勾掉一项会记入 override 但**不触发整体重算**：重算会把树折叠回去，用户连续勾掉几十项时那很难用。override 已记下，下次重算自然生效
  - **拖拽改类目（需求 10.11）尚未实现**。模型已开好 `ItemIsDragEnabled` / `ItemIsDropEnabled` 的 flag，但 `dropMimeData` 与视图侧的 drop 处理还没写。当前可通过「勾掉 + 在别处调整」间接达到目的，但这不等价，需补
  - `conflict` 不为 `none` 的节点显示橙色角标（`#F59E0B`）
  - 目标文件名与源文件名不同时该行显示 `旧名 → 新名`
  - 支持把文件拖拽到另一个类目节点，更新 `category_id` 与 `target` 并记入 override
  - 支持复选框批量排除条目（`included` 置 false）
  - _需求: 10.5, 10.6, 10.10, 10.11, 10.15_
  - _属性: 2, 18_

- [x] 28. 实现文件详情面板
  - **已完成。** `app/ui/widgets/detail_panel.py`
  - 原路径与目标路径完整显示且可选中复制：用户核对某个具体文件去哪了的时候，省略号中间的路径没有用
  - 状态行汇总置信度、AI 标记、自动改名、冲突、未勾选、读取问题，冲突时整行转橙
  - 图片走 `QPixmap` 缩略图，`text_head` 非空时显示正文前 6 行（M5 接入正文提取后自然生效）
  - 选中文件时右栏显示原路径、目标路径、命中规则、大小、修改时间
  - 图片文件显示缩略图；`text_head` 非空时显示正文前若干行（正文提取在 M5 接入）
  - AI 分配的条目显示 AI 角标（M5 接入数据来源）
  - _需求: 10.7, 10.8, 10.9, 8.7_

- [x] 29. 实现搜索筛选与底部操作条
  - **已完成。** 名称搜索、只看冲突、只看未分类、整理前/整理后切换
  - 「模拟运行」放在「开始整理」左边且是常规按钮、后者是主按钮——视觉上先引导到安全动作
  - **选择「覆盖」策略时立即弹确认**（需求 9.6），文案说明它是三种策略里唯一会动到已有文件的一种，用户不确认就回退到默认策略
  - **空目录清理开关 false → true 时弹警告**（需求 20.3、20.4），三条说明齐备，取消则开关回弹
  - 「开始整理」与「模拟运行」当前只弹提示说明 M4 未接入——**没有撤销的执行不该被允许**，两者必须一起落地
  - 按文件名搜索过滤，`只看冲突` 与 `只看未分类` 两个筛选开关
  - 底部操作条：冲突策略下拉、空目录清理开关（M5 接入行为）、`模拟运行`、`开始整理`
  - `overwrite` 冲突策略需二次确认后才允许启动执行
  - _需求: 10.12, 9.6_

- [ ] 30. 实现 M3 属性测试
  - 属性 18 与 19（预览编辑动作与方案状态一致性）、属性 20（override 叠加与朴素实现一致）、属性 21（六场景重算保留 override）、属性 22（override 优先性与失效处理）、属性 23（重算稳定性）、属性 41（视觉令牌取值与控件存在性）
  - 属性 41 只断言令牌取值与控件存在性，不做像素级断言；UI 状态联动用 pytest-qt
  - _需求: 10.1-10.16, 19.1-19.14_
  - _属性: 18, 19, 20, 21, 22, 23, 41_

---

## M4 执行与撤销

交付：`Executor`、`Journal`、`UndoManager`、`HistoryManager`、执行结果页、历史记录页。
本阶段应通过属性：6（不含清理维度）、12（不含 LLM 维度）、24、25、26、27、28、29、31、32、33、34、36。

- [ ] 31. 实现 Journal
  - 在 `core/journal.py` 实现 `manifest.json` 写入：完整 `SortPlan`、源树快照（path、size、mtime）、`scan.scope`、已勾选子文件夹清单、冲突策略、`remove_empty_dirs`、生效的 `OverrideSet`、`run_id`、app 版本
  - 实现 `journal.jsonl` 追加写：动手前写 `intent`，操作结束写 `done` 或 `failed`，每条写入后对文件描述符 `fsync`
  - 主副本写 `%APPDATA%\DocSorter\history\<run_id>\`，根目录 `.docsort\` 写镜像副本
  - `JournalRecord` 与 `Manifest` 的序列化复用 `to_jsonable()` / `from_jsonable()`
  - _需求: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.9, 13.10, 19.11_
  - _属性: 28, 40_

- [ ] 32. 实现 Executor 的执行语义
  - 在 `core/executor.py` 实现单线程顺序执行（保证 journal `seq` 全序与撤销逆序可靠，刻意不并发）
  - 同卷：`os.replace` 原子重命名
  - 跨卷：复制到目标 → 校验 size 与 sha256 一致 → 源文件移入回收站；校验失败则删除已复制的目标、保留源文件、该条目标记 `failed`
  - **不使用 `shutil.move`**：它跨卷时先复制后 `unlink`，源文件不可回收，校验失败也保不住源
  - `overwrite` 策略下被覆盖文件先移入回收站并在 journal 记录该动作
  - 目标类目目录不存在时创建并记 `created_dir`
  - 单文件操作抛异常时标记 `failed` 并继续处理其余条目；`included` 为 false 的条目跳过
  - 进度经 `ProgressThrottle` 200ms 批量回调
  - 实现 `dry_run=True` 模式：产出与真实执行结构相同的报告，且不改动任何文件与目录
  - _需求: 10.10, 11.8, 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 12.7, 12.8_
  - _属性: 24, 25, 26, 27_

- [ ] 33. 实现执行前的防护与可取消
  - 首次对某根目录触发执行时先强制运行一次模拟运行并展示结果，之后才允许真实执行
  - 确认对话框显示具体数字与可撤销说明，例如「将移动 1,240 个文件到 8 个文件夹，此操作可完整撤销」
  - 确认后提供 3 秒取消窗口与大号取消按钮；窗口内取消则放弃执行、全部文件位置不变
  - 执行中提供「停止」动作，在当前单文件操作完成后生效，停止后提供「回滚已完成部分」
  - _需求: 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8_
  - _属性: 29_

- [ ] 34. 实现 UndoManager
  - 在 `core/undo.py` 实现三阶段撤销，顺序固定不可调换：① 按 `removed_dir` 逆序重建目录 → ② 按 `done` 记录逆序还原文件 → ③ 删除 journal 中标记 `created_dir` 且当前为空的目录
  - 同卷原操作用反向 `os.replace` 还原；跨卷原操作把目标文件复制回源路径、校验一致后删除目标副本
  - 还原前比对文件当前 size 与 mtime 和 journal 记录值，不一致则跳过、列入「需人工确认」并提供源与目标的并排对比信息
  - 撤销过程本身写独立 journal 记录，使重做等价于「撤销的撤销」，走同一套代码
  - `created_dir` 采用删除语义、`removed_dir` 采用重建语义，两类记录不得混用
  - _需求: 14.2, 14.3, 14.4, 14.5, 14.6, 14.7, 14.8, 14.9, 14.12, 14.13_
  - _属性: 31, 32, 33, 34_

- [ ] 35. 实现 HistoryManager
  - 在 `core/history.py` 实现 run 列表读取、`RunMeta` 维护、任意未撤销 run 的撤销入口
  - 保留策略默认 20 条 / 30 天，超出上限或超期时删除最旧 run 目录；条数与天数可配置
  - 实现未收尾 run 检测：存在 `intent` 记录但缺少对应 `done` 或 `failed` 记录即为未收尾
  - _需求: 13.7, 15.2, 15.3, 15.4, 15.5_
  - _属性: 36_

- [ ] 36. 实现 ExecuteService 与 UndoService
  - 在 `services/execute_service.py` 与 `services/undo_service.py` 把 `Executor` 与 `UndoManager` 放进 `QThread`，桥接进度与停止信号
  - 两者线程模型同构：单线程顺序执行
  - _需求: 12.7, 11.6, 11.7_

- [ ] 37. 实现执行结果页
  - 执行中显示进度条与实时日志表
  - 结束后按成功、跳过、失败三组展示报告并显示各组计数
  - 提供「撤销本次整理」并把 Ctrl+Z 绑定到该动作、「重做」（撤销完成后出现）、「打开目录」（资源管理器定位根目录）
  - 导出 CSV，字段为源路径、目标路径、动作、结果、理由；行数等于三组计数之和（由 `to_csv_rows()` 保证）
  - _需求: 14.1, 14.10, 16.1, 16.2, 16.3, 16.4, 16.5_

- [ ] 38. 实现历史记录页
  - 时间轴形式列出每条 run 的时间、根目录、文件数、策略、撤销按钮与撤销状态
  - 已撤销的 run 标记「已撤销」并禁用其撤销按钮
  - _需求: 15.1, 15.2, 15.6_

- [ ] 39. 实现崩溃恢复流程
  - 启动时调用 `HistoryManager` 的未收尾检测，存在未收尾 run 时弹对话框提供「恢复执行」「全部撤销」「忽略」三个选项
  - 选择「全部撤销」时还原全部 `done` 操作，并把停留在 `intent` 状态的条目列入「需人工确认」列表
  - _需求: 13.7, 13.8, 14.11_
  - _属性: 33_

- [ ] 40. 实现 M4 属性测试
  - 属性 6（配置组合下的方案与执行一致性，暂不含清理维度）、属性 12（方案完整覆盖，暂不含 LLM 维度）、属性 24 至 27（执行语义、内容不变性、跨卷校验、回收站集合）、属性 28（journal 往返与 fsync 顺序）、属性 29（执行前防护与取消）、属性 31 至 34（撤销往返、外部修改拒绝覆盖、崩溃恢复、撤销重做幂等）、属性 36（历史保留策略）
  - 跨卷分支用 `VolumeStub` 模拟，不依赖真实第二个卷；回收站用 `TrashRecorder` 断言路径集合；保留策略用 `FakeClock`
  - 中断位置生成器覆盖首条前、中间随机条、末条后
  - _需求: 11.2-11.8, 12.1-12.8, 13.1-13.10, 14.2-14.13, 15.2-15.4_
  - _属性: 6, 12, 24, 25, 26, 27, 28, 29, 31, 32, 33, 34, 36_

- [ ] 41. 实现撤销专项测试
  - 执行进程被强制终止后重启，断言未收尾 run 被正确识别且「全部撤销」能完成回滚
  - 移动后源文件被外部修改（改 size 或 mtime），断言撤销拒绝覆盖并列入「需人工确认」
  - 跨卷复制中途失败，断言源文件仍在（或在回收站）且目标副本已清理
  - 目标目录只读、源文件被占用，断言标记 `failed` 且不中断其余条目
  - 嵌套类目目录的创建与清理，断言 `created_dir` 只在为空时被删
  - 撤销 → 重做 → 再撤销，断言文件树状态与首次撤销一致
  - _需求: 12.3, 12.6, 13.7, 13.8, 14.6, 14.7, 14.11, 14.13_
  - _属性: 26, 32, 33, 34_

---

## M5 内容与 AI 与清理

交付：`ContentExtractor`、LLM 子系统、规则管理 UI、空目录清理。
本阶段应通过属性：11、12（含 LLM 维度）、13、14、15、6（含清理维度）、35、37、38、39。

- [ ] 42. 实现 ContentExtractor
  - 在 `core/inspector.py` 按扩展名分派读取器，每个读取器都是「读到 4096 字符就停」的惰性实现：pypdf 逐页累加、python-docx 逐段落累加、openpyxl 以 `read_only=True` 逐行取单元格文本、python-pptx 逐形状取 `text_frame`、纯文本先读前 64KB 字节再用 chardet 探测编码解码
  - 仅当扩展名属于 pdf/docx/xlsx/pptx/txt/md 且 size 小于 20MB 时提取，否则 `text_head` 保持为空
  - 任一读取器抛异常都捕获并写入 `error` 后继续处理其余文件
  - 在扫描线程内用 `ThreadPoolExecutor(max_workers=8)` 并发；提交前检查 `CancelToken`
  - _需求: 5.1, 5.2, 5.3, 5.4, 5.6_
  - _属性: 5, 11_

- [ ] 43. 实现 by_content 分类器
  - `text_head` 非空时在其上应用关键词规则并产出类目建议，接入 `智能` 策略管线的第二位
  - _需求: 3.6, 5.5_
  - _属性: 11_

- [ ] 44. 实现 Provider 抽象与两个实现
  - 在 `core/llm/provider.py` 定义 `Provider` 协议与统一的五类失败分类：超时、非 2xx、schema 不符、额度耗尽、网络错误
  - `core/llm/openai_compat.py` 实现 `OpenAICompatProvider(base_url, api_key, model)`，覆盖所有兼容 `/v1/chat/completions` 的服务
  - `core/llm/ollama.py` 实现 `OllamaProvider(host, model)`
  - 单次请求超时取 `ai.timeout_seconds`（默认 30 秒）；调用统一使用 `temperature=0` 与 JSON schema 结构化输出
  - 实现「测试连通性」：发起一次最小请求并在 10 秒内返回成功或失败原因
  - 在 `tests/fixtures/` 实现 `FakeProvider`：按脚本返回 taxonomy 与 assignments，可注入五类失败，记录调用次数
  - _需求: 7.1, 7.4, 7.8, 6.8_
  - _属性: 12, 15_

- [ ] 45. 实现两阶段 taxonomy 调用与收敛
  - 在 `core/llm/taxonomy.py` 实现第一阶段：抽样不超过 300 个文件摘要请求模型产出 taxonomy
  - 约束 taxonomy 类目数不超过 12 且层级深度不超过 2
  - 用编辑距离与同义词表把相近类目名归并（发票 / 发票单 / 电子发票 / 票据归为一类），归并函数必须幂等
  - 归并后仍超过 12 时，按文件数从少到多合并进 `其他` 直至不超过 12
  - 第二阶段：全部文件摘要按每批 80 至 120 条分批发送，提示中要求模型只能从固定 taxonomy 中选或返回 `unknown`；各批交集为空且并集等于全集
  - 模型返回不属于 taxonomy 的类目时把该条目 confidence 置为 0，交由后续分类器处理
  - 实现 `by_llm.py` 接入 `智能` 策略管线的第三位
  - _需求: 7.5, 7.6, 7.7, 7.9, 7.10, 7.11_
  - _属性: 13_

- [ ] 46. 实现 LLM 批级缓存
  - 在 `core/llm/cache.py` 用 SQLite 存批级结果，key 为文件摘要集合的哈希，命中即复用并跳过该批请求
  - 缓存值的序列化复用 `to_jsonable()` / `from_jsonable()`
  - 缓存命中返回的类目分配必须与首次调用产出的一致
  - 实现预估请求次数计算，且预估值与实际批数一致
  - _需求: 8.5, 8.6, 8.9_
  - _属性: 13, 15_

- [ ] 47. 实现隐私分档与凭据存储
  - `仅元数据`（默认）档只发送文件名、扩展名、大小、修改时间与去掉根目录前缀的相对路径
  - `元数据+正文前500字` 档额外发送 `text_head` 的前 500 字符，切换到该档必须要求用户显式勾选确认后才保存
  - 构造请求时以相对路径替换绝对路径，使请求体序列化文本不含根目录字符串
  - 通过 keyring 把 api_key 写入 Windows 凭据管理器；`settings.yaml` 只写 keyring 引用键，不落明文
  - _需求: 7.2, 7.3, 8.1, 8.2, 8.3, 8.4_
  - _属性: 14_

- [ ] 48. 实现 AI 开关、降级与可解释性 UI
  - 设置页总开关与方案预览页顶部即时开关作用于同一 `ai.enabled` 配置项；未配置可用 Provider 时开关渲染为禁用
  - `ai.enabled` 为 false 时管线跳过 `by_llm`，Provider 调用次数为 0
  - 五类失败统一处理：记一条错误日志 → 采用规则引擎结果完成分类 → UI 显示「AI 分类不可用，已使用规则分类」；任何失败下 `SortPlan` 仍覆盖全部 `FileEntry`
  - 切换 AI 开关时按需求 19 的重算流程重新生成方案并保留 override，完成后刷新预览树
  - LLM 分配的条目在预览树显示 AI 角标；`confidence` 小于 0.6 的条目 `included` 置为 false
  - `ai.enabled` 为 true 且方案未生成时显示待处理文件总数与预估请求次数
  - _需求: 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 8.6, 8.7, 8.8_
  - _属性: 12, 15_

- [ ] 49. 实现空目录清理的候选推导与执行
  - 在 `core/planner.py` 实现 `EmptyDirPredictor` 为**纯函数**：候选集合 = 本次 run `done` 移动记录的源父目录 ∩ 已勾选子文件夹闭包 − 根目录，按深度降序排列
  - 确认对话框的预测清单与执行末段的实际删除**共用这一个函数**，不写两份判定逻辑
  - 在 `Executor` 末段（全部文件操作结束后）执行清理，删除同时满足四条件的目录：位于根目录内、是至少一条 `done` 移动记录的源父目录、结束后既无文件也无子目录、不是根目录本身
  - 空判定固定用 `fsops.is_dir_empty()`（`next(os.scandir(d), None) is None`）单一口径，天然同时满足「唯一口径」与「仅含 `desktop.ini` / `Thumbs.db` 视为非空」
  - 执行前已为空且本次未移出文件的目录保留；`scan.scope` 为 `top_level_only` 时保留根目录内全部子文件夹；未勾选参与整理的子文件夹及其内部全部目录保留；根目录本身在任何配置组合下保留
  - 用 `os.rmdir` 删除，每次删除追加 `removed_dir` 记录并 `fsync`；`remove_empty_dirs` 为 false 时保留根目录内全部目录
  - `dry_run` 时把将被删除的目录填入 `predicted_removed_dirs` 并保持全部目录存在
  - 在 `tests/fixtures/models.py` 实现参照实现 `naive_expected_removed_dirs(before_tree, moved_records, selection)`，作为需求 20 四条件的直译，与生产代码的推导路径完全独立
  - _需求: 20.5, 20.6, 20.8, 20.9, 20.10, 20.11, 20.12, 20.13, 20.14, 20.15, 20.16, 20.17, 20.18, 20.20_
  - _属性: 37, 38, 39_

- [ ] 50. 实现空目录清理的 UI 与警告对话框
  - 方案预览页底部操作条的开关文案固定为「清理整理后变空的子文件夹」
  - `false → true` 切换先弹警告对话框，正文三段分别说明「只删除本次整理中被移空的子文件夹」「原有文件夹结构会因此改变」「该删除可通过撤销恢复」，并要求显式确认；用户取消则开关回弹且配置保持 false
  - 开关状态在同一会话内不重复警告，但每次由 false 切到 true 都会警告
  - 执行确认对话框显示预计被删除的空目录数量与完整清单，数据取自任务 49 的纯函数
  - _需求: 20.2, 20.3, 20.4, 20.7_

- [ ] 51. 实现规则管理页
  - 展示与编辑关键词规则与扩展名规则（id、type、priority、patterns、category、enabled）
  - 保存后下一次方案生成即使用修改后的规则
  - 解析错误时展示含行号与字段名的结构化错误提示
  - _需求: 4.3, 4.8_

- [ ] 52. 实现设置页
  - 分组呈现扫描、分类、执行、AI、历史保留、清理各组配置项
  - AI 分组含 Provider 切换（`OpenAICompatProvider` / `OllamaProvider`）、base_url / host / model / api_key 输入、「测试连通性」按钮、隐私分档选择、`ai.enabled` 总开关
  - 历史保留分组含保留条数与保留天数
  - _需求: 6.2, 7.1, 7.4, 8.3, 15.5, 18.1, 18.8_

- [ ] 53. 实现 M5 属性测试
  - 属性 11（正文提取门槛、上限与编码正确性）、属性 12 补齐 LLM 维度、属性 13（批处理与 taxonomy 收敛）、属性 14（隐私与凭据不外泄）、属性 15（Provider 调用计数与缓存一致性）、属性 6 补齐清理维度、属性 35（外部干扰下的行为）、属性 37 至 39（空目录删除集合精确性、预测与实删一致、清理后撤销恢复）
  - 生成器需覆盖：文件内容维度（0 字节、utf-8/gbk/utf-16、二进制、>4KB、>20MB）、模型返回维度（合法 taxonomy、超 12 类、深度 3 级、近义重名、taxonomy 外类目、非法 JSON、五类失败）、目录形状维度（执行前就已为空、只含子目录、仅含 `desktop.ini` / `Thumbs.db`）
  - 凭据相关属性用 `MemoryKeyring` 替身，使其在无凭据管理器的环境下也能跑
  - _需求: 5.1-5.6, 6.4-6.6, 7.3, 7.5-7.11, 8.1-8.9, 20.8-20.21_
  - _属性: 6, 11, 12, 13, 14, 15, 35, 37, 38, 39_

---

## M6 打磨与打包

交付：视觉细节、空态/骨架屏/错误态、PyInstaller 打包与首次运行释放。
本阶段任务：属性 41 复核 + 基准测试 + 冒烟测试。

- [ ] 54. 实现空态、骨架屏与错误态
  - 各页面无数据时显示空态视图并给出下一步动作提示（含根目录下没有任何孤立文件的情形，此时引导用户去子文件夹选择器）
  - 扫描进行中显示骨架屏
  - 任一步骤出错时显示错误态视图，含错误摘要与重试动作
  - _需求: 17.9, 17.10, 17.11_

- [ ] 55. 复核视觉令牌落地
  - 逐项核对主色 `#2563EB`、圆角 8px、间距栅格 4/8/12/16/24、状态色 `#16A34A` / `#F59E0B` / `#DC2626`、字体 Segoe UI + 微软雅黑 UI、深浅主题跟随系统、类目 chip 10 色轮转
  - 复核任务 9 的守卫测试仍然通过：`qfluentwidgets` 的 import 未泄漏到 `pages/` 与 `widgets/`
  - _需求: 17.4, 17.5, 17.6, 17.7, 17.8_
  - _属性: 41_

- [ ] 56. 实现基准测试
  - 用 pytest-benchmark 标记 `slow`（不入门禁）测量三条性能阈值：默认范围（孤立文件 ≤ 5000）1 秒内完成扫描、勾选致孤立文件总数 10 万时 3 秒内完成扫描、预览树 10 万条目滚动与展开响应 100ms 内
  - 记录实测数值，不作为通过门禁
  - _需求: 2.14, 2.15, 10.14_

- [ ] 57. 实现集成测试与冒烟测试
  - 集成测试覆盖属性测试刻意排除的外部依赖，能力不可用的环境下 skip：符号链接与 junction 不递归、回收站语义、keyring 读写、Provider 连通性、系统主题跟随、services 层线程归属
  - 冒烟测试：首次运行释放 `rules_default.yaml` 到 `%APPDATA%\DocSorter\rules.yaml`、主窗口可构造、`python -m app.core.cli` 可跑通
  - _需求: 1.6, 1.7, 4.1, 7.2, 7.4, 17.3, 17.7_

- [ ] 58. 【需用户决策】确认组件库授权口径
  - PyQt/PySide-Fluent-Widgets 系列在 PyPI 标注 GPLv3，作者另售商业授权。用户尚未表态，打包前必须定档
  - 四条可选路径：仅自用（无影响）/ 以 GPLv3 开源本工具 / 购买商业授权 / 更换组件库
  - 若选择更换组件库，改动范围限于任务 9 建立的 `app/ui/theme/` 薄封装层，`pages/` 与 `widgets/` 不需改动
  - 该项定档后才执行任务 59
  - _需求: 18.4_

- [ ] 59. 实现 PyInstaller 打包
  - 编写 PyInstaller spec：`--noconsole`、图标、以 `--add-data` 把 `rules_default.yaml` 纳入包内
  - 加断言测试：spec 的 datas 项包含 `rules_default.yaml`
  - 裁剪未使用的 Qt 模块以控制体积
  - 真实打包与首次运行验证由用户手动执行，命令与结果反馈回来后再收口
  - _需求: 18.6_
