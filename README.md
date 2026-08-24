# DocSorter 文档分类工具

Windows 桌面端工具，把指定目录下散乱的文件按可解释的规则自动归入分类文件夹。

主流程四步：**选择目录 → 扫描分析 → 生成方案并允许修改 → 确认后执行**。

## 三条产品红线

这三条是设计里不可让步的部分，其余功能都围着它们让路。

**1. 误操作必须可完整复原。** 默认动作是移动，一次误点可能打散上千文件，所以撤销是一等公民：同卷用 `os.replace` 原子重命名，跨卷先复制、校验大小与 SHA-256、再把源文件移入回收站；双份落盘日志（`%APPDATA%` 与根目录 `.docsort\`）；崩溃后可恢复或整体退回；撤销前校验文件是否被外部改动过；历史时间轴上任意一条都可撤销。

**2. AI 分类是可选增强，永不成为主流程的单点故障。** `ai.enabled` 默认关闭。任何超时、额度耗尽、非法返回都自动回落到规则引擎，并在界面上说明「AI 分类不可用，已使用规则分类」。

**3. 默认只动根目录下的孤立文件，目录结构默认不被改动。** 子文件夹自身及其内部内容不在默认整理范围内；只有逐级手动勾选过的子文件夹，其内部的孤立文件才参与整理。根目录内的任何子文件夹默认都不会被删除——无论是否为空。只有完成两层勾选（先勾选该子文件夹参与整理，再勾选「清理整理后变空的子文件夹」并通过警告确认）之后，它才可能因本次整理而变空并被清理，且该清理同样可撤销。

## 运行

需要 Python 3.12。

```powershell
python -m pip install -r requirements.txt
python -m app.main
```

无界面的命令行入口可以在不加载 Qt 的情况下跑通「准入 → 扫描 → 分类 → 规划 → 冲突预检」整条链：

```powershell
python -m app.core.cli --root <目录> --strategy type_and_date --dry-run
```

## 配置与数据位置

| 路径 | 内容 |
| --- | --- |
| `%APPDATA%\DocSorter\settings.yaml` | 全部配置项 |
| `%APPDATA%\DocSorter\rules.yaml` | 分类规则，首次运行从内置规则释放，可自行编辑 |
| `%APPDATA%\DocSorter\history\<run_id>\` | 每次执行的 `manifest.json` 与 `journal.jsonl`，撤销依据 |
| `%APPDATA%\DocSorter\llm_cache.db` | LLM 批级缓存 |
| `<根目录>\.docsort\` | 该次执行日志的镜像副本 |
| Windows 凭据管理器 | API key。配置文件里只存 keyring 引用，不存明文 |

规则文件写坏了不会让工具不可用：解析失败会给出行号与字段名，并回落到内置默认规则完成本次分类。

## 分类策略

- **按文件类型**（默认）：文件名关键词 → 扩展名
- **按时间**：按修改时间，粒度为年或年-月
- **类型+时间**：两级类目
- **智能**：文件名关键词 → 正文关键词 → LLM → 扩展名

智能策略需要在设置页填好模型服务参数并打开 AI 开关；未配置可用 Provider 时开关是灰的。支持 OpenAI 兼容服务与本地 Ollama。

隐私默认只发送文件名、扩展名、大小、修改时间和去掉根目录前缀的相对路径。要额外发送正文前 500 字，必须在设置页显式确认。

## 开发

```powershell
# 门禁：排除基准测试
python -m pytest -m "not slow"

# 基准测试（会真的建 10 万个文件，需要几分钟）
python -m pytest tests/benchmarks -m slow -q -s

# 重新生成应用图标
python tools/make_icon.py

# 打包
python -m PyInstaller --clean --noconfirm docsorter.spec
```

分层约束由 `tests/unit/test_layering.py` 静态守卫：

- `app/core` 不 import Qt、不 import `app.services` / `app.ui` / `app.config`，因此 42 条正确性属性不需要 `QApplication` 就能跑
- 只有 `app/ui/theme/components.py` 允许 import `qfluentwidgets`，pages 与 widgets 一律从 `app.ui.theme` 取组件

正确性属性测试在 `tests/properties/`，命名对应 `design.md` 的属性编号。

## 已知差距

预览树承载 10 万条目时，两种操作超出 100ms 响应预算：单个类目上万成员时最慢一批懒加载约 700ms；把滚动条直接拖到最末端约 200ms。常规滚动不受影响（约 12ms），展开与视图切换也在预算内。原因是 `QTreeView` 对已展开分支的重布局代价随该分支可见行数增长，与模型侧无关。要达标需要给大类目分桶使任一分支的可见行数有上界。`tests/benchmarks/` 里用 `xfail` 记录了这两项及其实测数值。

## 授权

界面基于 [PySide6-Fluent-Widgets](https://github.com/zhiyiYo/PyQt-Fluent-Widgets)，该组件库在 PyPI 标注 GPLv3（作者另售商业授权）。

**本项目定档为仅自用、不分发。** GPL 的义务由分发行为触发，不分发就不产生开源、购买商业授权或更换组件库的义务，因此仓库不放 `LICENSE`——放了反而等于声明了一个与「不分发」不符的对外授权口径。

由此确定两条边界：

- 不把构建产物、安装包或源码交付给任何第三方，包括网盘公开链接、应用商店、公开代码仓库
- 一旦要分发，必须先重新定档（见 `.kiro/specs/document-sorting-tool/tasks.md` 任务 58）。届时更换组件库的改动面收敛在 `app/ui/theme/`

Qt 绑定用的是 LGPL 授权的 PySide6，不受上述约束。
