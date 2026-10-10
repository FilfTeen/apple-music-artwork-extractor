# 维护与贡献

从 [README](README.md) 了解安装与使用，从 [技术说明](docs/TECHNICAL.md) 查找模块职责、结果协议和验证入口。支持范围与已知限制见 [STATUS](docs/STATUS.md)，版本变化见 [CHANGELOG](CHANGELOG.md)。

## 开发环境

需要 Python 3.11+。在独立虚拟环境中安装开发及可选依赖：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev,motion,ui]'
python -B -m unittest discover -s tests -v
```

默认测试使用合成输入，不访问 Apple 服务。真实媒体工具集成另需本机 FFmpeg 与 ffprobe；调用方式见 [技术说明](docs/TECHNICAL.md#验证入口)。

## 文件职责

| 位置 | 用途 |
| --- | --- |
| `src/apple_music_artwork/` | 包实现；纯解析与网络、文件写入保持分离 |
| `tests/`、`tests/fixtures/` | 离线回归与最小合成数据 |
| `examples/` | 可直接运行的离线输入示例 |
| `src/apple_music_artwork/resources/apple-artwork-operator/` | 随包提供的 Agent 运行指南 |
| `docs/` | 技术说明、接口说明与支持范围 |
| `pyproject.toml`、`MANIFEST.in` | 分发元数据、依赖、命令入口和源码包范围 |
| `.github/workflows/ci.yml` | 基础依赖矩阵、安装件检查与媒体工具集成 |

行为变化应更新对应测试和使用说明。新 fixture 应说明用途，使用最小合成数据；不得加入凭据、私人账号数据、完整服务响应或 Apple 媒体二进制。版本变化写入 CHANGELOG，支持范围变化时同步 STATUS。普通文档修订无需改变产品版本。

## 检查安装件

构建实际分发文件：

```sh
python -m build
```

在新的虚拟环境安装生成的 wheel，确认实际导入来自安装目录，然后检查两个命令别名、模块入口、`capabilities`、`doctor` 和 README 中的离线示例。源码测试通过不能替代 wheel 安装检查。

依赖组合分别验证：Python 3.11、3.12、3.13、3.14 的基础安装，以及 Python 3.12 的 `ui`、`motion`、`ui,motion`。基础环境保持没有 m3u8 和媒体工具，需要 m3u8 的用例在 motion 组合执行；CI 配置给出具体分组。

修改终端交互时，检查窄屏与宽屏、中文长名称、调整窗口大小、无颜色、纯文本和重定向。菜单应覆盖有效选择、EOF、超时与取消；机器调用应检查 stdout 单 JSON 和 stderr JSONL 的分离。

修改任务状态或恢复逻辑时，用合成任务检查只读查询、首次升级、升级前副本保留、完成文件复用、损坏状态拒绝和版本兼容。媒体集成检查原字节、实际媒体信息、完整解码、截断拒绝及原目录复用。实时 Apple 检查应与离线回归分开，报告资源、地区语言、时间和实际检查范围。

## 提交变更

提交前检查工作树与暂存内容，只纳入本次修改：

```sh
git status --short
git diff
git diff --cached
```

下载媒体、任务状态、构建输出、虚拟环境和本机凭据不应加入版本历史。报告修改内容、执行的检查、结果及尚未验证的范围，避免把配置可解析或文件存在当作功能验证通过。
