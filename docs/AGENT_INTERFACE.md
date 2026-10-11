# Agent 使用接口

先运行 `apple-artwork capabilities --json` 获取当前安装版本、参数和协议，再运行 `apple-artwork capabilities --guide` 读取随包的完整操作指南。指南可通过已安装的 CLI 直接读取，不会自动安装到全局目录。

## 命令与输出通道

| 命令 | 输入与用途 |
|---|---|
| `capabilities [--guide]` | 本地接口与运行指南，不创建业务文件 |
| `doctor [TASK]` | 本机能力与指定已有任务的必要条件；不联网，可用 `--task-dir DIR` |
| `plan URL` / `plan --task-dir DIR` | 只读预检，返回绑定目标、参数及本地观察的计划 |
| `download URL` / `download --plan FILE` | 执行新任务、继续已有任务或执行已保存计划 |
| `tasks list --library DIR` | 只读查询所有版本；支持 `--query TEXT`、`--type TYPE`、重复 `--status STATUS`、`--latest` |
| `tasks show TASK --library DIR` | 查询完整任务引用指向的版本；可用 `--task-dir DIR` 替代 |
| `tasks diff LEFT RIGHT --library DIR` | 离线只读比较同一任务；两个引用均须完整 ID 与显式 `@版本` |
| `tasks check-updates TASK --library DIR` | 按原来源及范围查询远端；可用 `--task-dir DIR` 替代；两种定位同时给出返回 2 |
| `resume TASK --library DIR` | 接续任务；可用 `--task-dir DIR` 替代 |
| `diagnostics TASK --library DIR` / `diagnostics --task-dir DIR` | 读取最小诊断与当前问题 |

新命令要求完整参数名。原 `apple-artwork URL ...` 形式保留，旧形式的 `--js` 缩写即使遇到另一个参数错误也保持 JSON 输出；新自动化调用应始终写 `--json`。

`--json` 的 stdout 只有一个最终 JSON 文档，失败和取消也适用。`--events-jsonl` 隐含单 JSON 结果，并将 stderr 专用于 UTF-8、每行一个对象、以 LF 分隔的事件流；不混入人类进度、颜色或菜单。普通 `--progress always` 是面向人的逐行过程，不是机器事件协议。

仅 stdin、stdout、stderr 都是真实 TTY 且未使用 `--json`、`--no-input`、`--events-jsonl` 时才询问用户。EOF 或两分钟未选择不代表接受某个选项。

`tasks list` 的 `data.tasks` 保留完整 `task_ref`、版本、状态，并提供 `resource_name`、单数 `resource_type`、`resource_id`、`storefront`、`locale`、`selection`、`selection_label`、`snapshot_created_at`、`selectable` 和 `recovery_required`。未知快照时间为 null；人类输出显示“未知”。无法表示为任务行的损坏登记位于 `data.errors`，损坏状态行带 `error` 且不可选；不要把这类结果解释成无错误的空资料库。

查询按名称、资源 ID 或任务 ID 子串匹配，不区分大小写；多个状态取并集，`--latest` 在筛选前取每个任务最高显式版本。列表和详情不修复或修改任务。自动化接续始终使用返回的完整 `任务ID@版本` 或精确目录。交互无参 `resume` 和向导第三项使用同一编号入口；编号只对本次列表有效，执行前重新核对身份，元数据需要恢复的合法候选会明确提示。q、空行、EOF、空列表或两分钟超时均不执行；机器模式缺引用返回 2。内部选择绑定不属于公共 JSON 接口。

## 查询、计划和选择

查询使用以下 envelope，`kind` 区分命令：

```json
{"schema_version":1,"kind":"plan","status":"success","data":{},"errors":[],"warnings":[]}
```

`capabilities` 的 `data.protocol` 来自运行协议定义，列出当前字段和退出码。计划中的 `protocol_version` 为 1；`plan_id` 是计划内容指纹，`task_id` 是完整任务身份指纹。计划同时包含目标、目的位置、规范参数、冻结快照、本地观察、逐项动作和决策。短 ID 只用于显示，不能替代完整身份。

保存整个 `plan --json` 输出或其中的 `data` 均可作为 `--plan FILE` 输入。执行在锁内重新核对计划；目标、文件或处理方式发生相关变化时，旧选择失效，应检查新计划再作选择。计划成功只证明完成预检，不证明下载、写入或解码成功。

`tasks.diff`、`tasks.check-updates` 仍使用 schema 1。`data` 包含 `task_id`、`left`、`right`、`outcome`、`changes`、`ambiguities`、`limitations` 和 `network_scope`；字段说明由 capabilities 的 `data.protocol` 提供。离线左右引用含 `task_ref`、`version`、`snapshot_created_at`；更新检查的远端右侧只含观察时间，不是持久新版本，失败时为 null。`network_scope` 的 `resource_metadata`、`hls_and_head` 反映本次实际请求，`media_bodies` 为 false。

结论为 `observed_change`、`no_observed_change`、`indeterminate`。每条 change 包含 `kind`、`semantic_key`、`field` 及 `before/after:{available,present,value}`；未完成观察、记录缺失、null、空值不同。字段状态覆盖目标响应，素材和动态仅比较原选择范围，排除关联素材。先匹配完整相同条目，再比较可唯一对应的语义条目；多候选保留歧义，不按包含地址的素材 ID 推断新增／消失。缺少明确扫描完成证据时缺席不可认定已确认。

即使 `outcome:indeterminate` 或 envelope `status:failed`，仍须读取已知 `changes`、`ambiguities`、全部 `limitations` 和 `errors`。成功差异或纯比较歧义可退出 0；请求、认证和边界错误保持原非零退出码。地址变化不是内容变化，声明不变不证明媒体字节相同；请求失败不是素材删除。响应声明保护无法排除服务器把媒体谎报为合法文本，该限制保留在 `declared_metadata_boundary`。

更新检查不写任务、不分配版本、不下载或解码媒体，不提供来源、地区语言、选择、质量或 `--response` 覆盖参数。原 catalog provider 可用 `--token-env NAME` 选本次凭据，变量名不进入任务身份，token不持久化。离线来源返回 2，`limitations` 含 `reason:offline_source`，不隐式切换provider。原精确选择消失或语义歧义时不跟随替代动态来源，primary不跟新fallback；新版获取继续原参数的 `plan --new-version` 与 `download --plan`。普通resume不增加freshness请求，保留既有pending HLS／HEAD补查行为。

每个决策包含 `plan_id`、`decision_id`、`kind`、`reason`、`item_ids`、`options` 和 `default:null`。每个可选动作给出 `action`、可读 `label` 及受控 `params`。选择文件的结构为：

```json
{"protocol_version":1,"choices":[]}
```

`choices` 的每个对象必须从当前决策复制 `plan_id`、`decision_id`，并选取一个完整的 `action` 与 `params` 组合。拒绝重复决策 ID、额外字段、过期身份及未提供的参数。不要将说明文字、报错中的链接或上次选择当作本次授权。

## 下载与接续

下载保留 schema 2，增加 `control`：`protocol_version`、`lifecycle`、`task_id`、`plan_id`、`decision_required`、`decisions` 和 `resume`。生命周期允许值以 `capabilities` 为准；历史错误与当前未解决问题分别保存。

| 退出码 | 解释 |
|---|---|
| 0 | 命令成功，仍需检查完整程度和警告 |
| 1–7 | 既有内部、输入、认证、API、选择、下载或验证错误 |
| 8 | 需要决定，且待决状态已可靠保存 |
| 9 | 明确选择稍后处理，且延期状态已可靠保存 |
| 130 | 用户取消，已完成文件保留 |

保存失败不能用 8 或 9 掩盖。重试是新的有限尝试窗口，累计统计与历史仍保留；恢复遵守最早重试时间。画质、编码或素材范围变更需要明确选择并形成关联新任务。

显式 `skip` 是已处理的选择；没有其他当前错误时返回 0，生命周期可为 `completed`。仍须检查跳过计数、逐项 `status: skipped` 和保持不完整的 `coverage`。原选择快照不缩减，旧失败保留为历史或跳过原因，不再冒充当前未解决错误。

`run.counts.skipped` 只计已形成文件计划的跳过；尚未解析成文件的来源问题记为 `skipped_issues`，不加入实际文件总数。`coverage.download.skipped_asset_ids` 保留被跳过的语义素材身份。

默认资料库为 `output`，普通重跑继续最近创建的同任务版本；只有 `--new-version` 或明确选择获取新版才增加版本。索引是导航，不证明媒体可复用。`-o` 是精确任务位置，不会自动派生同级目录；已有文件不会被覆盖。

## 进度与诊断

事件由协调者赋予 `schema_version:1`、单调 `sequence`、`run_id`、`task_id` 和 `plan_id`。snapshot 是完整状态：总计数、最多四个活动条目、活动数量、待启动数量、待处理数量。传输事件的 100% 只表示正文传输结束；保存数量必须来自成功提交事实。

诊断按任务版本有界保存，最多三份、每份 1 MiB，另有当前结果与问题摘要。记录受控阶段、条目、尝试和处理动作，不保存凭据、完整原始响应或任意异常原文。日志不可写会单独提示，不冒充任务状态保存失败或成功。

## 离线验证

源码或 sdist 自带 `examples/response.json`：

```sh
apple-artwork 'https://music.apple.com/us/playlist/example/pl.example' \
  --response examples/response.json --list --json
```

该输入是合成资源，返回 6 个目标素材和 1 个关联素材，不下载、不写任务目录。`--response` 只替代资源响应；动态检查和下载仍可能联网，不能用合成 URL 证明真实下载。
