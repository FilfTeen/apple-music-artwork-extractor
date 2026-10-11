# 技术结构

## 包与依赖

- 分发名：`apple-music-artwork`。
- Python 模块：`apple_music_artwork`，放在 `src/` 下。
- CLI：`apple-artwork` 与 `apple-music-artwork` 指向同一个 `cli:main`。
- Python 3.11+；Pillow 负责图像解码，标准库承担 CLI、HTTP 和测试。
- `motion` extra 提供 `m3u8>=6,<7`，只在动态操作时加载；动态下载另需本机 FFmpeg 和 ffprobe，静态功能不依赖它们。
- `ui` extra 提供 `rich>=14.1,<15`，只负责终端呈现。没有 Rich 时所有命令和协议仍可使用。
- 构建采用 setuptools，`dev` extra 提供 build frontend；运行依赖与开发工具分开。

`pyproject.toml` 是分发元数据、入口和依赖的配置源。包的 `__version__` 与已安装分发版本由 CI 一致性检查保护。

## 数据流与职责

```text
URL / 本地响应
  -> 资源身份解析
  -> 目标身份、地区来源与字段覆盖
  -> 素材发现、分类与语义选择
  -> 图片 rendition / HLS 档位及完整 MP4 布局
  -> 冻结任务计划 / 核对原目录恢复记录
  -> 按实际请求分组、受限并行下载、串行视频验证
  -> 持久化待提交记录、无覆盖发布、清单与完整程度
```

纯解析和选择函数不访问网络或文件系统。CLI 负责参数与呈现，执行协调者组织一次命令；HTTP、认证、响应大小限制和文件提交位于可替换的边界。

| 模块 | 职责 |
| --- | --- |
| `models.py` / `errors.py` | 资源、素材、观察、选择、rendition 和稳定错误结构 |
| `urls.py` | 公开 URL、storefront、locale、album 的 `?i=` song 目标解析 |
| `discovery.py` | list/map 适配，目标与关联素材分离、来源证据与字段状态 |
| `selection.py` | 默认主封面、精确语义/ID、全部静态/动态/媒体选择 |
| `rendition.py` | 来源分组标识、模板替换、固定修饰和尺寸约束 |
| `jsonio.py` | 有界 JSON 文件读取、严格输入、确定性输出 |
| `client.py` | 可注入 HTTP，catalog/Web provider、认证、有限重试与流式下载 |
| `runtime.py` | 命令共享取消、请求预算、同源冷却、运行内缓存和事件队列 |
| `hls.py` | 档位、质量策略、单 MP4 全区间覆盖检查 |
| `download.py` | Pillow 解码、色彩证据、目录相对写入和不覆盖提交 |
| `motion.py` | 本机工具发现、MP4 信息核对与完整视频解码 |
| `execution.py` | 计划、恢复、分组、地区比较、有界工作线程和清单协调 |
| `run_state.py` | 固定目录锁、任务检查点、持久化文件和清单事务 |
| `cli.py` / `__main__.py` | 参数、执行入口、最终结果和退出码 |
| `presentation.py` | 中文帮助以外的阶段进度、最终摘要和安全下一步 |
| `workflow.py` / `planning.py` | 命令路由、只读预检、计划核验、决策和接续 |
| `task_queries.py` | 只读任务筛选、坏登记呈现与内部不可变选择绑定；pending候选只读完整核验 |
| `comparison.py` / `remote_check.py` | 纯本地快照差异、原选择范围的远端观察；保留未知与歧义，不请求媒体正文 |
| `library.py` / `protocol.py` | 任务版本导航、持久分配及版本化查询/选择契约 |
| `interaction.py` / `commands.py` | 有界终端输入、能力自描述及独立 JSONL 通道 |
| `diagnostics.py` | 本机能力检查、受控事件投影和有界诊断 |

## 表示约定

- 输入为 UTF-8 JSON，拒绝重复对象键、非有限数字、非法 UTF-8 和无法解析的结构；普通空白与键顺序不影响接受。输出为排序键、缩进、终止 LF 的 JSON，不将这种展示形式宣称为唯一 canonical byte form。
- 素材字段缺失、未知字段、空值与已知的不存在应分别处理，不用空结果吞掉结构错误。
- 声明尺寸、请求尺寸和实际解码尺寸分别记录。请求更大 rendition 不等于源图有更多真实细节。
- 相同 source 不等于相同渲染字节。保留模板中的 `SC.DN01`、`SHT.AMTPPS01`、`sr` 等固定修饰；不把它们当成无关参数删除。
- 主封面仅在同优先级候选中按同 source 消歧。全部静态素材保留不同 rendition；下载按解析后的请求 URL、尺寸与格式识别重复，同时保留语义与 provenance。
- 资源身份校验不能只依赖响应遍历顺序；关联资源的素材不能混入目标资源结果。
- 请求地区不作为响应地区的自证。输入 storefront、locale 保持原样；可靠资源 `href` 作为地区证据，仅页面 URL 不足以证明。已证明冲突可供 list 查看，但阻止下载选择；缺少证据保持未核实。
- 动态按 master URL 合并检查，再按实际 MP4 URL 和长度合并下载并保留引用。同组尺寸、编码、帧率或 HLS 时长声明冲突会产生 `MOTION_DECLARATION_CONFLICT`，不下载该组。
- 原始凭据和认证 header 不进入日志、异常输出或结果 JSON。

## 静态与动态质量

图片请求不得放大。`--all-media` 对 `{f}` 模板默认 PNG；旧默认模式和 `--all-static` 仍默认 JPEG。固定扩展名不被 `--format` 改写。图像验证还记录模式、透明度、ICC 与 sRGB 证据；`hasP3` 是来源声明，PNG 解码成功不自动证明 P3 或更多源细节，冲突声明全部保留。

动态 `best` 先比较声明像素数、帧率，同条件优先 HEVC，再比较同编码码率；`compatible` 只选 H.264。I-frame 不参与普通视频选择；HDR 标签记录但不推导质量优先级。图片宽高、格式选项不改变动态文件，不同动画即使同为方形也保留。

只支持已结束、无加密、无不连续、无低延迟或 delta 部分结构的单 MP4 布局。初始化区间与全部媒体区间必须从 0 开始连续覆盖同一对象，并与 HEAD 长度一致。多文件、未知编码、替代音视频组、缺失关键档位声明和非完整布局明确失败，不静默降级。

通过检查后下载原 MP4；ffprobe 核对容器、单视频轨、编码、尺寸和帧率，并扫描所有 packet 的整数 PTS、DTS、duration 与 time_base。DTS 按解码顺序连续；B 帧允许该顺序中的 PTS 前后跳动，但按 PTS 排序后的呈现区间必须连续，无间隙或重叠，每个 packet（含末包）都有明确正时长。不具备这些证据时拒绝，不泛化支持所有 VFR 或时间轴结构。

`duration_seconds` 以完整 packet 呈现区间的跨度为准，`duration_basis=complete_packet_presentation_span`；容器和流报告的 start/duration 单独保留为 `timing_declarations`。带非零起点的容器值不能直接当作内容时长，也不能只靠二者相减接受文件。实际跨度与 HLS 核对后，FFmpeg 完整解码，要求解码帧数等于 packet 数、无丢帧或补帧，解码进度终点与已验证跨度的差异不得超过现有时长容差 `max(0.1 秒, 2 / 平均帧率)`。不同 FFmpeg 版本的进度原值可能表示最后帧起点或呈现末端，完整性须结合帧数、packet 时间轴与完整解码共同判断。全部通过才提交。

HLS `FRAME-RATE` 是声明上限。实际峰值由连续呈现区间中最短 packet 时长的倒数计算，并以 `validation.frame_rate.observed_maximum` 记录；`average` 和 `nominal` 保留 ffprobe 的平均与基础帧率报告，`nominal` 不可用时为 null；基础帧率估计不再独自决定接受与否。支持范围与已知限制见 [STATUS.md](STATUS.md)；程序不转码、不改写容器，也不把 HLS、HEAD 或初次 metadata 当作完整解码证明。

## JSON 与清单

list 与下载结果使用 `schema_version: 2`；查询命令使用 schema 1 envelope。查询、计划、选择文件与完整命令协议见 [Agent 接口](AGENT_INTERFACE.md)。下载结果包含：

| 字段 | 含义 |
| --- | --- |
| `target` | 输入 URL、storefront/resource type/resource ID、locale、可选 album ID |
| `source_evidence` | 请求地区、观察地区及 verified/unverified/conflict 证据 |
| `resource_name` | 响应中的资源名称 |
| `assets` / `related_assets` | 目标拥有的素材 / 关联资源拥有的素材；后者不参与目标下载 |
| `warnings` / `errors` | 稳定 code 与可展示 message；错误还含 category、retryable |
| `mode` / `provider` | list 或 download；web、catalog 或 offline |
| `selection` | list 时为 null；下载时含 policy、selected、reason、rejected、references |
| `downloads` | 下载尝试、相对文件名、来源、请求与验证结果、保留的语义引用 |
| `motion_inspections` | master、档位、所选布局、支持状态与引用，尚未检查正文 |
| `coverage` | 请求观察、字段状态、扫描、检查、下载、验证与完整程度 |
| `comparison` | 地区比较时各地区独立结果、素材及字段差异 |
| `status` | 命令执行的 success、partial 或 failed |
| `run` | 本次运行身份、快照、并发数、文件计数、请求统计和取消状态 |
| `control` | 任务生命周期、计划/任务身份、待决事项与恢复入口 |

素材记录包含 `asset_id`、`owner`、`semantic_path`、`kind`、`role`、`shape`、`url`、`source_identity`、声明尺寸、traits、declarations 和 observations。`observations` 保留 request variant 与 JSON pointer；asset ID 基于资源、语义、rendition 和已有声明计算，与观察顺序无关。它不等于文件字节哈希。

`coverage.fields` 区分 missing、null、empty、present、malformed；响应观察不一致时保留 mixed。未返回字段不等于确认无素材。未知候选、扫描受限、来源未核实、检查失败、不支持或未验证素材都会阻止完整标记。

`coverage.completeness` 仅适用于指定资源、地区、语言、已返回字段与支持的布局。执行成功不表示完整：例如 list 不取正文，成功的默认主封面下载也不声称已导出全部媒体。

下载结果分别记录 declared/requested/actual dimensions，以及 decoded format、bytes、SHA-256。磁盘 `manifest.json` 的 `status_scope=download_results` 表示它记录下载尝试；它不能证明自身已保存。最终 stdout JSON 的 `manifest_report` 与进程退出码报告清单提交结果。部分文件提交后发生错误时保留已完成文件并返回非零，不悄悄回滚或覆盖；必要的残留以相对文件名报告。

schema 2 中成功项的 `status` 仍为 `downloaded`，用 `action=downloaded|reused` 区分本次新取和本地复用。复用的 `validation` 是原验证证据，`local_verification` 记录本次核验，不能据原 `full_decode=true` 声称本次重新解码。视频验证策略为 `mp4-packet-full-decode-v2`；旧策略完成文件首次接续须本地完整复验，成功后推进策略而保留原 `validation`。此次证据写入 `local_verification.validation`，后续常规复用通过 `local_verification.previous_full_validation` 保留最近一次完整核验记录，并将本次 `decoded_this_run` 记为 false。

`run` 的 `run_id` 标识本次执行，`task_fingerprint` 标识冻结任务，`resumed` 标记恢复。`snapshot.created_at` 是原采集时间，旧清单缺失时为 null；`snapshot.remote_freshness=not_rechecked` 专指恢复时没有重新获取资源目录信息。`pending_inspection={attempted,completed,checked_at}` 披露对上次未完成动态规格的补查；已有所选档位只继续它的媒体播放列表，已有 MP4 布局只补其 HEAD，不回退重选 master。

`run.counts` 分别记录 `total`、`new_downloaded`、`reused`、`failed`、`cancelled`、`not_started` 和 `semantic_references`。文件计数按冻结的实际请求去重，引用数另计；清单和错误顺序不受线程完成顺序影响。`run.stats` 的 `requests` 是实际请求数，`received_bytes` 是交给应用的 HTTP 正文字节（包含失败尝试收到的部分），`cache_hits` 是命令内请求缓存命中数；不含 HTTP/TLS 头，也不把本地复用字节计作网络流量。

## 恢复、文件事务与取消

`plan` 只读核对本地观察、任务差异、依赖和资源估算；不建立业务目录或恢复状态。计划指纹绑定身份和动作，`--decisions` 的选择必须同时匹配计划 ID、决策 ID 和明确提供的参数组合。执行持锁后再次检查相关观察，过期选择不能直接执行。

默认资料库使用资源类型、资源名/ID、storefront/locale、选择/任务短 ID/版本分层。普通重跑选择最新明确创建的同任务版本；新版本分配先持久保存，再更新导航索引，索引不是媒体复用证据。`-o` 保持精确目录语义。

新任务媒体布局版本为 2，图片和视频分别通过受限 `images`、`videos` 子目录访问。旧 flat 布局保留。恢复状态独立升级到 v2，升级前原件保存在独立的 `state-v1-original.json`；只读查询不迁移，滚动保存不能取代这份原始副本。

下载使用 `.apple-artwork-state/` 保存原资源快照、计划、文件状态及清单版本。任务身份覆盖资源、地区、语言、provider、选择规则及媒体参数；并发数、进度模式、JSON 展示不属于任务身份。相同任务复用完整文件前核对长度、SHA-256、请求约束与验证策略；缺失文件重取，损坏文件保留并按项冲突。无法识别的状态、清单或不一致任务要求新目录。

固定锁文件使用非阻塞排他锁，持有至工作线程及自有媒体进程全部结束。锁文件不会被删除或通过 PID 推测来抢占。选择或依赖失败前可能已建立状态目录和锁，但不会因此发布业务媒体或清单。

协调者登记每个临时文件及身份。文件验证通过后先持久化待提交记录，再原子发布、同步目录并记为已提交；恢复可识别文件已发布而检查点未更新的窗口。媒体目标始终不覆盖。清单先登记新版本及哈希，原子替换已识别清单并保留上一版本；恢复只接受已登记的旧版或待发布版。未知残留保留并报告，不能借清理流程删除来源不明的文件。这不承诺任意断电环境下绝不丢失。

一个协调者独占状态、清单与终端，默认两个工作线程（`--jobs` 范围 1–4）处理独立文件。等待视频验证也占文件名额；探测和完整解码用一个共享名额串行。Ctrl-C 停止新派发、唤醒等待、合作取消传输并回收自有媒体进程；已验证成果完成必要提交，收齐工作线程后保存状态与部分清单，以 130 退出。最终短暂收尾和输出期间忽略重复 SIGINT，输出结束即恢复原处理器。

严格可重建身份的 v0.2 在线清单首次导入须完成当前完整本地验证后才登记策略；不能重建必要字段的清单拒绝自动导入。后续兼容策略下只做本地核验，不反复解码。全部完成的目录可零网络复用；未完成的 HLS/HEAD 检查仅继续冻结来源的首次检查，不重新获取 catalog 或替换既有具体请求。

## 网络与写入边界

默认 Web provider 从公开网页同源脚本发现 WebPlay 候选，在请求 API 成功后才缓存于当前进程。候选头部检查不等于密码学验证。catalog provider 读取显式环境变量中的开发者 token；两者都不使用浏览器登录态、cookie 或隐式代理。

HTTP 只使用 HTTPS，每次重定向重新检查目标 host；认证请求拒绝跨 origin 重定向。命令共用默认 64 次真实请求预算，重试和重定向均计入；一次逻辑请求重定向最多 5 次，明确瞬态失败最多尝试 3 次。socket 超时最多 20 秒且不超过剩余期限。元数据单次逻辑请求总期限 60 秒，媒体传输总期限 300 秒，均包含重试、冷却和重定向。

传输中断可在新临时文件从头重试，不拼接部分正文；整文件重试和连接重试共用尝试预算，不叠加多层。协议、身份和媒体验证错误不自动重试。有效 `Retry-After` 不截短，超过剩余期限时报 `RETRY_AFTER_DEADLINE`，结果给出等待秒数和 Unix `retry_at`，中文提示给出 UTC 时间。全部等待可取消，同源冷却由所有工作线程共享。

每次命令复用一个 `AppleMusicClient`；资源发现与凭据刷新串行。成功的 HLS/HEAD 按完整 URL、方法和请求上下文合并及缓存，失败不缓存；缓存仅存在于当前命令。工作线程使用独立 transport，共享控制器，不共享媒体正文缓存。

单 HLS 文本最多 1 MiB、128 个档位、1024 个媒体段；单 MP4 最多 256 MiB。工具版本检查、probe、完整解码分别最多 10、30、300 秒，probe/decode 诊断输出最多 1 MiB。

媒体工具从 PATH 查找，或由 `APPLE_ARTWORK_FFMPEG`、`APPLE_ARTWORK_FFPROBE` 指定绝对路径。工具以 argv 数组运行，只接收本地临时文件，输入协议限制为 file；远程 URL 不交给 FFmpeg。业务清单记录工具名与版本，不记录本机工具路径或原始诊断。

JSON/API 与图像响应默认上限 32 MiB，图像解码上限 4000 万像素。Pillow 先 verify，再重新打开并 load；校验静态帧数、实际格式、Content-Type 与请求尺寸。不把 motion preview 尺寸当作视频流尺寸。

下载需要 macOS/Linux 的 `dir_fd`、no-follow、目录及文件锁支持。输出路径拒绝 `..` 与符号链接遍历；媒体写入已登记的独有临时文件，再以原子 hard link 提交，已有媒体目标不会被覆盖。适用于操作者管理的输出目录；不宣称对任意敌对并发目录操作有通用安全保证。

## 退出码与故障处理

| 退出码 | 类别 | 常见处理 |
| --- | --- | --- |
| 0 | 执行成功 | 仍需按用途检查 warnings 与 completeness |
| 1 | 内部错误 | 保留 code 和复现步骤，运行离线测试 |
| 2 | 输入错误 | 检查 URL、JSON 和互斥参数；查看 `--help` |
| 3 | 认证错误 | catalog 检查环境变量；Web 可能已改变 bootstrap |
| 4 | API/资源错误 | 检查资源在指定 storefront 是否可用 |
| 5 | 发现/选择错误 | 用 `--list --json` 查看素材；歧义时选择精确 ID |
| 6 | rendition/下载错误 | 检查依赖、声明、支持布局、网络和新输出目录 |
| 7 | 媒体验证错误 | 检查格式、尺寸、时长或解码失败；不使用失败临时文件 |
| 8 | 已保存，等待决定 | 查看当前 `control.decisions` 或 `tasks show`，明确选择后 resume |
| 9 | 已保存，明确延期 | 尊重最早重试时间，稍后使用 resume |
| 130 | 用户取消 | 等待收尾后，以原命令与原目录恢复；最新资源用新目录 |

普通 `--list --response FILE` 可完全离线；加上 `--inspect-motion` 会请求 HLS/HEAD，下载模式会请求媒体。`--list` 不执行选择，不能和五个选择开关混用。地区比较只允许在线 list，必须显式固定 `l`，最多 8 地区；单份响应不能代表多个地区。

`--jobs` 只用于下载，显式 `--list --jobs 2` 同样拒绝。`--progress auto` 仅 stderr 为 TTY 时显示；`always` 支持无 ANSI 逐行日志；`never` 只关闭过程提示。TTY 最多 4 Hz，重定向连续进度最多 1 Hz；同一窗口内的近期阶段边界保留待刷，TTY 优先展示当前阶段。stdout 只输出最终文本或单个 JSON。下载总量未知时无百分比，不显示 ETA；传输完成后仍显示验证和保存，最终文件计数只接受成功提交事实。

多条目视图消费协调者的完整 snapshot，最多显示四个活动条目；每次刷新重新读取终端宽度，支持中文显示宽度。无 Rich 时使用追加式文本，避免窗口缩放后按过期行数清屏。Rich Live 使用 `auto_refresh=False`、`redirect_stdout=False`、`redirect_stderr=False`，不引入后台刷新写者；依据 [Rich Live 官方说明](https://rich.readthedocs.io/en/stable/live.html)。`--plain` 完全禁用动态行与颜色，`--no-color` 或 `NO_COLOR` 仅禁颜色。

只有三个标准流均为 TTY 且未启用 JSON、JSONL 或 `--no-input` 才建立菜单。输入最多等待 120 秒；没有选择返回待决，显式 defer 返回延期。退出 8/9 均以状态可靠保存为前提，任务身份或写入失败保留实际错误类别。诊断写失败单独提示，不能替代状态持久化判定。

## 验证入口

安装开发及动态依赖后运行：

```sh
python -m pip install -e '.[dev,motion,ui]'
python -B -m unittest discover -s tests -v
python -m build
```

标准库 unittest 覆盖 URL、身份、来源、选择、模板、颜色、HLS、网络边界、真实图像解码、媒体工具协议、无覆盖写入和部分失败。工具替身不作为真实视频解码证据。

独立的 `tests/test_cli_media_tools.py` 使用两份自有 64×64 H.264：2 fps、2 帧，以及 24 fps、48 帧、非零起点且带 B 帧的分片 MP4，每个输入最多 1 MiB。建立专用输出根后运行：

```sh
python tests/test_cli_media_tools.py \
  --input-video /absolute/path/to/owned-test.mp4 \
  --bframes-video /absolute/path/to/owned-offset-bframes.mp4 \
  --output-root /absolute/path/to/test-output \
  --require-installed
```

可加 `--ffmpeg`、`--ffprobe` 绝对路径。先用真实 ffprobe 确认样本的帧数、起点、B 帧重排、连续时间轴，以及容器时长不等于呈现跨度，再向 CLI 注入合成 HTTP，真实执行 HLS、PNG 解码、probe/完整解码与写入。检查原字节/哈希、去重引用、清单及同目录零请求/零重复媒体进程复用。截断样本的 HEAD 与实际长度一致，须由真实媒体验证拒绝，同时保留成功图片。全部不访问 Apple。`--require-installed` 拒绝当前源码导入。

CI 使用本机 libx264 生成两份片段，再给自有 fMP4 的 `tfdt` 字段增加 10 秒偏移并去掉过时的可选末尾 `mfra` 索引；逐字核对压缩媒体 payload 未改。产品下载不会做这些容器修改。测试样本必须经实际特征检查后才能用于验收，不将 Apple 媒体二进制作为正式 fixtures。

CI 静态 job 只装基础依赖，并将该测试进程的 PATH 指向自有空目录，确认 m3u8、FFmpeg、ffprobe 均不可用；明确分离需真实 m3u8 的 `test_hls`、`test_execution_motion` 、`test_execution_runtime.ExecutionMotionRuntimeTests`、`test_execution_v04.MotionDecisionTests`、`test_remote_check.MotionObservationTests` 和 `test_cli_v05.CliUpdateMotionFailureTests`（保留同模块的静态执行与比较测试），其余回归包含动态依赖缺失失败路径。媒体 job 安装 motion extra 与 FFmpeg，运行完整 suite 和上述实际工具集成。两者测试构建 wheel 的安装件。

检查包括合成输入的成功与失败路径、真实 wheel/sdist 和安装后的 CLI。实时 Apple 媒体验证应独立说明样本、地区语言、时间和实际检查范围；配置可解析不能替代安装件或目标平台的运行结果。

## 构建资料

- [setuptools 的 pyproject 配置](https://setuptools.pypa.io/en/latest/userguide/pyproject_config.html)
- [setuptools 的分发文件控制](https://setuptools.pypa.io/en/latest/userguide/miscellaneous.html)
- [Pillow Image API](https://pillow.readthedocs.io/en/stable/reference/Image.html)

维护者修改依赖、入口或模块职责时，应同时检查本页、包元数据和对应测试。

## 任务查询与版本比较

任务列表和编号选择共用只读查询；最高显式版本先于过滤，坏行与分配错误分别报告。编号绑定本次完整引用和内部不可变身份观察，执行在恢复写之前及真实持锁后核对；过期观察不修复、不重建丢失目录。合法已登记pending候选可显示“需要恢复元数据”，完成任务仍沿原快照复用。

版本比较先配对完整可比投影相同的条目，再比较唯一语义对应；多个候选报告歧义。字段缺失、null、空值、未知观察与已确认不存在保持区别；缺少明确完成的扫描证据不能确认素材新增或消失。`tasks check-updates` 固定原provider、地区语言及选择；catalog凭据环境变量可本次指定而不写入身份。新鲜HTTP视图不修改父缓存，必要HLS/HEAD计入本次实际网络范围，失败保留已知差异及原错误码；远端观察没有虚构的持久版本。
