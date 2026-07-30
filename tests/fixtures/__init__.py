"""属性测试共用的生成器与替身。

生成器（任务 12 实现 trees.py）必须覆盖 design.md「共用的生成器与替身」表中
列出的全部维度，缺一条就意味着对应属性形同虚设。

替身：
    FakeProvider    按脚本返回 taxonomy 与 assignments，可注入五类失败并记录调用次数
    FakeClock       注入进度节流器与保留策略，使时间相关逻辑可确定性断言
    VolumeStub      改写 fsops.same_volume 的判定源，让跨卷分支不依赖真实多卷环境
    TrashRecorder   替换 send2trash，记录被移入回收站的路径
    MemoryKeyring   替换 keyring 后端，使凭据相关属性在无凭据管理器时也能跑

models.py 放刻意写得笨但显然正确的参照实现，供 model-based 属性对照：
    naive_scan                  对照属性 3
    naive_expected_removed_dirs 对照属性 37
    naive_apply_overrides       对照属性 20 与 22
"""
