# Apple Music Artwork Extractor

从 Apple Music 公开链接发现视觉素材，按明确规则选择封面，下载并验证图片及支持的动态封面。支持 playlist、album、artist、song；single、EP、compilation 使用 album 资源族。

v0.5 提供可读任务导航、编号接续、离线版本差异与只读远端更新检查，保留引导式终端、预检、版本资料库及 Agent 使用协议。动态下载支持播放列表明确描述的完整单文件 MP4；不支持的布局会留在结果中。默认 Web provider 使用实验性 Apple Music Web 接口，支持范围与已知限制见 [STATUS.md](docs/STATUS.md)。

## 安装

需要 Python 3.11+。下载功能需要 macOS 或 Linux 的目录描述符能力；Windows 下载尚不支持。静态功能只依赖 Pillow。

从 GitHub Release 下载 `apple_music_artwork-0.5.1-py3-none-any.whl` 后，可在新的虚拟环境中安装：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ./apple_music_artwork-0.5.1-py3-none-any.whl
apple-artwork doctor
```

使用同一 wheel 安装可选终端界面及动态解析依赖：

```sh
python -m pip install './apple_music_artwork-0.5.1-py3-none-any.whl[ui,motion]'
```

也可在完整源码目录执行：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
apple-artwork --help
```

`apple-music-artwork` 是同一命令的别名，也可运行 `python -m apple_music_artwork`。

从源码安装时，在源码目录中添加动态检查或下载所需的可选解析依赖：

```sh
python -m pip install '.[motion]'
```

动态下载还需要本机可用的 `ffmpeg` 和 `ffprobe`。使用适合本机架构、来源可信的安装方式，参见 [FFmpeg 下载入口](https://ffmpeg.org/download.html)。程序从 PATH 查找它们，也可通过 `APPLE_ARTWORK_FFMPEG` 和 `APPLE_ARTWORK_FFPROBE` 提供绝对路径。`--list --inspect-motion` 不需要这两个工具；静态功能不需要任何动态依赖。

## 先离线检查

运行 `apple-artwork` 可进入简短向导；在管道或其他非交互环境中，无参数只显示简明用法。可选终端界面依赖提供更平稳的多条目刷新。从源码安装时，在源码目录执行：

```sh
python -m pip install '.[ui]'
apple-artwork doctor
```

`ui` 不改变下载和验证能力，未安装时仍可使用全部命令。从源码安装时，需要完整界面及动态能力可在源码目录安装 `.[ui,motion]`。

以下示例无需连接 Apple，也不会写文件：

```sh
apple-artwork 'https://music.apple.com/us/playlist/example/pl.example' \
  --response examples/response.json --list --json
```

预期结果为 6 个目标资源素材、1 个关联歌曲素材，`downloads` 为空。示例没有真实地区来源证明，因此会显示未核实状态。身份与素材地址均为合成数据，不能用于真实下载。

## 检查和下载真实资源

建议先预检再下载。`plan` 不建立任务目录，不下载媒体正文；必要时会读取资源信息、HLS 和文件头，并说明联网范围：

```sh
apple-artwork plan 'APPLE_MUSIC_URL' --all-media
apple-artwork download 'APPLE_MUSIC_URL' --all-media
apple-artwork tasks list
apple-artwork resume TASK
```

`TASK` 使用查询结果中的完整任务引用。新命令要求完整参数名。原来的 URL 直接调用方式继续可用。

`tasks list` 只读显示名称、资源类型、地区与语言、选择范围、快照时间和完整 `任务ID@版本`；未知时间显示“未知”。可用 `--query TEXT` 按名称、资源 ID 或任务 ID 子串筛选，`--type album|artist|playlist|song` 按类型筛选，重复 `--status STATUS` 取状态并集。`--latest` 先取每个任务最高显式版本，再应用筛选；默认列出全部版本。损坏条目单独报告，其他任务仍可查看。 损坏的版本登记可能使同库任务接续被严格核验拒绝；须先核验保留的登记。

在三个标准流均为交互终端时，运行 `apple-artwork resume --library output` 可按编号选择本次列表中的明确版本，向导第三项也进入此入口。执行前会重新核对身份；“需要恢复元数据”的合法任务可选择，损坏条目不可选择。空列表、q、空行、EOF 或两分钟未选择均不接续。JSON、管道、`--no-input` 或 `--events-jsonl` 中须给出 `TASK` 或 `--task-dir DIR`，否则返回输入错误 2。

可用 `apple-artwork tasks diff TASK_ID@1 TASK_ID@2 --library output` 离线比较同一任务的两个明确版本；将 `TASK_ID` 替换为完整任务 ID。`apple-artwork tasks check-updates TASK --library output` 按原地区、语言、provider、选择范围和质量参数检查远端，也可改用 `--task-dir DIR` 精确定位，两种定位不能同时提供。catalog 可用 `--token-env NAME` 指定本次凭据环境变量；更新检查不能改来源、地区语言、选择或格式。

比较和更新检查均只读，不分配版本、不修改旧任务、不重新解码媒体。更新检查仅读取资源信息及必要的 HLS／HEAD，不请求媒体正文。结果区分“观察到变化”“未观察到变化”“无法判断”，即使无法判断也保留已知差异、歧义及限制。地址变化不证明内容变化，声明未变化不证明媒体字节相同，请求失败不解释为素材删除；服务器将媒体谎报为允许的文本仍无法由响应声明检查排除。`--json` 提供完整字段证据；比较成功或纯歧义返回 0，实际错误沿用原退出码。离线导入的任务无法自动切换远端来源，更新检查返回输入错误 2。获取新版继续使用原参数的 `plan --new-version` 与 `download --plan`；普通恢复继续原快照，不增加远端新鲜度请求。

将 `APPLE_MUSIC_URL` 替换为公开资源链接：

```sh
apple-artwork 'APPLE_MUSIC_URL' --list
apple-artwork 'APPLE_MUSIC_URL' --list --json
apple-artwork 'APPLE_MUSIC_URL' -o output/my-cover
apple-artwork 'APPLE_MUSIC_URL' --all-static -o output/all-images
apple-artwork 'APPLE_MUSIC_URL' --all-media -o output/all-media --json
apple-artwork 'APPLE_MUSIC_URL' --all-motion -o output/motion --json
apple-artwork 'APPLE_MUSIC_URL' --list --inspect-motion --json
apple-artwork 'APPLE_MUSIC_URL' --semantic editorialArtwork.staticDetailSquare -o output/editorial
```

默认选择规则：playlist 优先 `editorialArtwork.staticDetailSquare`，再用 `artwork`、`editorialArtwork.subscriptionCover`；album、artist、song 使用自身 `artwork`。规则会写入结果，不宣称全局最佳。等优先级出现不同来源时返回歧义错误，可用 `--asset-id` 精确指定。

| 选项 | 行为 |
| --- | --- |
| `--list` | 列出发现的目标视觉素材，不选择、不下载媒体正文、不写文件 |
| `--response FILE` | 从本地 JSON 读取资源信息；动态检查及下载模式仍可能访问 HLS 和媒体 CDN |
| `--semantic PATH` / `--asset-id ID` / `--all-static` / `--all-media` / `--all-motion` | 五选一；不能与 `--list` 合用；精确语义或 ID 可以选择动态源 |
| `--all-media` | 保留全部已识别静态呈现及独立动态源；图片 `{f}` 模板默认使用 PNG |
| `--all-motion` | 只下载全部已识别动态源 |
| `--width` / `--height` | 仅影响图片；只给一边时按声明比例计算另一边，不允许放大 |
| `--format jpg\|png\|webp\|avif` | 仅填充图片 `{f}`；显式参数优先，固定格式保持原样；旧静态模式默认 JPEG |
| `--motion-profile best\|compatible` | 默认 `best`；`compatible` 选择 H.264，不转码 |
| `--list --inspect-motion` | 请求有界 HLS 文本及 MP4 HEAD，列出档位和支持状态，不取媒体正文 |
| `--jobs 1\|2\|3\|4` | 默认 2，限制同时处理的独立文件数；等待验证也占名额；不能与 `--list` 合用 |
| `--progress auto\|always\|never` | stderr 的过程提示；默认 auto 仅在交互终端显示，always 支持逐行日志，never 关闭进度 |
| `--json` | stdout 输出单个 JSON 文档，失败也有结构化结果 |
| `--library DIR` | 资料库根目录，默认 `output`；与 `-o` 精确任务目录互斥 |
| `--new-version` | 明确获取新版并保留旧版；普通重跑不会新建版本 |
| `--plan FILE` / `--decisions FILE` | 执行已保存的计划，提交绑定当前计划和决策的选择 |
| `--no-input` | 不询问，可靠保存待决状态后以 8 返回 |
| `--plain` / `--no-color` | 纯文本输出 / 禁用颜色；也支持 `NO_COLOR` |
| `--events-jsonl` | stderr 专用于 JSONL 事件；stdout 保持单个 JSON，不等待输入 |

默认资料库按资源、地区语言和任务版本组织；图片与视频分开保存。以下资源名称和 ID 是虚构示例：

```text
output/album/Synthetic Album--123456789/us--en-US/
  all-media--<任务短ID>--v1/
    images/
    videos/
    manifest.json
    .apple-artwork-state/
```

名称只用于阅读；任务身份使用完整指纹，未指定语言会如实标记。普通重跑继续最近明确创建的同任务版本，包括尚未完成的版本；只有 `--new-version` 或明确选择“获取新版”才分配新版本。资料库索引损坏不会成为重复获取的理由，复用仍须核对文件。

`-o DIR` 始终指定精确任务目录，不会偷偷在旁边创建新版。新目录按 `images/`、`videos/` 分类；旧 flat 目录原地兼容，不自动搬移媒体。首次执行旧任务时在锁内升级恢复状态并单独保留升级前副本；只读查询不迁移。

相同任务继续原快照；已完成文件核对长度、SHA-256、请求和验证策略后复用，缺失或未完成文件从头补取。升级到 v0.5.1 后，旧验证策略的已完成视频首次接续会在本地重新探测和完整解码，不重新下载；成功后再次接续无需重复媒体进程。原下载验证证据保留，新核验单独记录；失败时保留现有文件并报告原因。清单保留选择原因、语义引用、最终 URL、声明/请求/实际尺寸、解码格式、字节数与 SHA-256。整个命令的最终结果以退出码及 JSON 中的 `manifest_report` 为准。

选择包含视频时，程序先检查动态依赖；缺失会在写入业务文件前失败。后续某个图片或视频失败时，会保留其他已完成文件和逐项结果，返回非零退出码。

## 等待、恢复与取消

终端按目标、预检、执行计划、进度、结果和下一步分区。进度显示总体计数、最多四个活动条目、待启动与待处理数量，区分下载、等待验证、验证、保存、复用、失败和跳过。下载到 100% 后仍须通过验证并成功提交；只有提交后的文件才计为完成。统计按去重后的实际文件计数，语义引用另计；未知总量不显示百分比，也不预测完成时间。视频探测和完整解码同一时间最多一个。

终端最多每秒刷新四次；`--progress always` 重定向为不含 ANSI 的逐行日志，连续进度最多每秒一条。人类提示为中文，参数名、错误码和 JSON 字段保持英文。`--json` 时 stdout 仍只有一个最终 JSON，过程提示写 stderr。

独立条目失败后先保留原因，其他不受影响的条目继续。到达决策点时停止派发并收齐活动工作，再提供当前允许的处理方式。重试开启新的有限尝试窗口；改变画质、编码或素材范围必须明确选择，并形成关联新任务。

菜单只有在三个标准流都为交互终端时启用；JSON、管道和 `--no-input` 均不等待输入。两分钟未选择或 EOF 不自动代选：可靠保存待决状态后返回 **8**。明确选择“稍后处理”并可靠保存后返回 **9**。恢复会遵守原先的最早重试时间，保存失败会返回真实错误，不冒充已挂起。

明确跳过后，结果单独列出跳过项与数量；没有其他当前错误时，命令可以成功结束。原选择范围仍保留，`coverage` 继续标记不完整，之前的失败留在历史中；退出 0 不表示原范围已经全部下载。

每个任务版本默认保存最多三份、每份 1 MiB 的结构化诊断，另有当前结果与未解决问题摘要。使用 `apple-artwork diagnostics --task-dir DIR --json` 查询；日志写入失败和任务状态保存失败会分别报告。

输出目录中的 `.apple-artwork-state/` 保存任务快照和恢复记录。同目录有运行中的任务会返回 `OUTPUT_BUSY`，请等待它结束，不要删除锁文件。资源、地区、语言、来源、选择或媒体参数不同时会拒绝混用目录；`--jobs`、`--progress`、`--json` 可以改变。被修改或损坏的媒体保留原件并按项报冲突，其他独立项继续；无法识别的状态或清单要求使用新目录。媒体文件始终不覆盖，工具仅更新已识别的自身清单。

全部完成后重跑会本地核验，不请求 Apple，也不重复兼容验证策略下的完整解码；结果用 `action: reused` 保留原验证记录，并另列本次 `local_verification`。如果上次已冻结来源、但 HLS 或 HEAD 检查因网络问题未完成，恢复可以补完该动态规格检查，并明确报告补查数量与时间。资源目录信息仍使用原快照；已选定的 MP4 请求不会静默换源、换档或换地区。

Ctrl-C 停止新任务并回收当前工作，已完成文件保留，收尾后退出 130。再次运行原命令和原目录即可继续；半文件从头重取，不使用字节范围续传。只清理已登记且身份一致的临时文件，未知残留会保留并报告。能够严格重建身份的 v0.2 在线清单可在首次完整本地验证后导入，旧清单未记录的采集时间显示“未知”。

## 动态质量与完整程度

`best` 按声明像素数、帧率排序，同条件优先 HEVC，再比较同编码码率。不同动画即使长宽比相同也保留；相同实际 MP4 请求只下载一次并保留语义引用。若同一请求的尺寸、编码、帧率或时长声明冲突，该组失败并保留证据。

支持的动态布局必须已结束、无加密，并且初始化区间和所有媒体区间连续覆盖同一个完整 MP4，长度与 HEAD 一致。下载原 MP4 字节后，`ffprobe` 核对媒体信息，FFmpeg 完整解码；通过后才提交文件，不重新编码。多文件 HLS、直播、加密、部分区间和低延迟分片会明确报告不支持。

结果采用 `schema_version: 2`。`status` 表示命令执行结果；`coverage` 分别记录字段未返回、`null`、空值、存在、异常，以及检查、下载和验证状态。未知候选、未检查项目或不支持的素材会阻止完整标记。完整程度只适用于指定资源、地区、语言、已检查响应及支持的布局。

最大已验证 rendition 不等于原始母版。PNG 也不自动证明更高源画质或 P3：结果保留 `hasP3` 声明及实际图像模式、透明度、ICC／sRGB 信息，对冲突或证据不足保持未核实。

## 比较地区

默认始终跟随链接的地区与语言。地区比较只用于 `--list`，要求 URL 带显式 `l` 参数，最多比较 8 个地区：

```sh
apple-artwork 'APPLE_MUSIC_URL?l=zh-Hans-CN' --list --compare-storefronts cn us --json
```

如果原链接已经有查询参数，请用 `&l=…`。原输入地区及各比较地区保留独立的来源、素材和字段状态，差异包含 URL、尺寸、traits 与色彩声明。一个地区失败不会丢弃其他地区结果。地区比较不能与单个 `--response` 文件混用。

## 官方 catalog provider

`--provider catalog` 使用官方 Apple Music API，需要你自己的开发者 token。由运行环境提供 `APPLE_MUSIC_DEVELOPER_TOKEN`，再执行：

```sh
apple-artwork 'APPLE_MUSIC_URL' --provider catalog --list --json
```

可用 `--token-env NAME` 指定另一个环境变量。token 不作为命令参数，也不写入结果。官方 catalog 的字段范围与实验性 Web 接口不同，不能保证同样的 editorial 或 motion 信息。

## 开发和验证

将工具交给 Agent 时，先运行：

```sh
apple-artwork capabilities --json
apple-artwork capabilities --guide
```

指南随安装包提供，包含能力检查、预检、明确决策、执行、结果检查和接续的完整用法。新查询接口为 schema 1，已有下载结果继续为 schema 2；详见 [Agent 接口说明](docs/AGENT_INTERFACE.md)。

```sh
python -m pip install -e '.[dev,motion,ui]'
python -B -m unittest discover -s tests -v
python -m build
```

依赖安装后，默认测试不访问 Apple 服务。CI 分别验证仅静态依赖和包含媒体工具的环境，构建 wheel/sdist 后测试安装件。在线验证与离线回归分开记录。

`tests/test_cli_media_tools.py` 是单独运行的媒体工具集成验收：向实际 CLI 注入合成响应、两帧 H.264 和非零起点 B 帧 MP4，真实探测及完整解码，再检查原目录复用和截断失败。调用方式与验证边界见 [TECHNICAL.md](docs/TECHNICAL.md)。

本工具不处理 Apple ID 登录、私人资料库、私人播放列表认证、音乐下载或 DRM 绕过。未知候选保留供检查，默认不会自动选择；缺少尺寸、无法解码或超限时明确失败。图片上限为 32 MiB／4000 万像素，单视频上限为 256 MiB。

- [模块、JSON、退出码与故障处理](docs/TECHNICAL.md)
- [Agent 接口与任务控制](docs/AGENT_INTERFACE.md)
- [支持范围与已知限制](docs/STATUS.md)
- [维护与贡献](CONTRIBUTING.md)
- [版本记录](CHANGELOG.md)

## 许可

本项目代码采用 [MIT 许可证](LICENSE)。Copyright (c) 2026 FilfTeen。
