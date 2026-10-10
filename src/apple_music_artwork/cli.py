"""Chinese CLI presentation around the command execution coordinator."""

from __future__ import annotations

import argparse
import os
import signal
import sys
from threading import current_thread, main_thread
from typing import Mapping, TextIO

from . import __version__
from .client import HttpClient, Transport
from .errors import ArtworkError
from .presentation import ProgressReporter, render_result
from .interaction import TerminalInteraction, can_interact
from .task_queries import query_tasks


EXIT_CODES = {"internal": 1, "input": 2, "auth": 3, "api": 4,
              "discovery": 5, "selection": 5, "rendition": 6,
              "download": 6, "validation": 7}


class _ArgumentError(ArtworkError):
    """A parser-owned correction, separate from arbitrary exception messages."""
    def __init__(self, hint):
        super().__init__("INVALID_ARGUMENTS", "Arguments do not satisfy the command requirements.", "input")
        self.hint = hint

    def to_dict(self):
        return {**super().to_dict(), "argument_hint": self.hint}


_DEFAULTS = {"url": None, "list": False, "response": None, "semantic": None, "asset_id": None,
             "all_static": False, "all_media": False, "all_motion": False, "inspect_motion": False,
             "compare_storefronts": None, "motion_profile": "best", "output": None, "library": None,
             "task_ref": None, "task_dir": None, "new_version": False, "plan_file": None,
             "decisions_file": None, "width": None, "height": None, "format": None, "provider": "web",
             "token_env": "APPLE_MUSIC_DEVELOPER_TOKEN", "jobs": None, "progress": "auto", "json": False,
             "no_input": False, "plain": False, "no_color": False, "events_jsonl": False, "guide": False,
             "query": None, "resource_type": None, "statuses": (), "latest": False,
             "left_ref": None, "right_ref": None}
_DISPLAY_OPTIONS = {"progress", "json", "no_input", "plain", "no_color", "events_jsonl"}
_RESOURCE_OPTIONS = {"response", "semantic", "asset_id", "all_static", "all_media", "all_motion",
                     "motion_profile", "width", "height", "format", "provider", "token_env"}
_COMMAND_OPTIONS = {
    "download": _RESOURCE_OPTIONS | {"list", "inspect_motion", "compare_storefronts", "output", "library",
                                     "new_version", "plan_file", "decisions_file", "jobs"},
    "plan": _RESOURCE_OPTIONS | {"output", "library", "new_version", "plan_file", "task_dir"},
    "doctor": {"library", "task_dir"},
    "tasks.list": {"library", "query", "resource_type", "statuses", "latest"}, "tasks.show": {"library", "task_dir"},
    "tasks.diff": {"library"}, "tasks.check-updates": {"library", "task_dir", "token_env"},
    "resume": {"library", "task_dir", "jobs", "plan_file", "decisions_file"},
    "diagnostics": {"library", "task_dir"}, "capabilities": {"guide"},
}


class _ScopedArguments:
    """Build only meaningful options; inactive destinations retain defaults."""
    def __init__(self, group, allowed, *, exclusive=False):
        self.group, self.allowed, self.exclusive = group, allowed, exclusive
        self.exclusive_group = None

    def add_argument(self, *flags, **kwargs):
        name = kwargs.get("dest") or next((flag[2:].replace("-", "_") for flag in flags if flag.startswith("--")), flags[0])
        if name not in self.allowed:
            return None
        group = self.group
        if self.exclusive:
            if self.exclusive_group is None:
                self.exclusive_group = group.add_mutually_exclusive_group()
            group = self.exclusive_group
        return group.add_argument(*flags, **kwargs)

    def add_mutually_exclusive_group(self):
        return _ScopedArguments(self.group, self.allowed, exclusive=True)


class _Parser(argparse.ArgumentParser):
    def __init__(self, *, stdout, stderr, **kwargs):
        super().__init__(**kwargs)
        self.output_stream, self.error_stream = stdout, stderr
        self._positionals.title = "位置参数"
        self._optionals.title = "选项"

    def error(self, message):
        # Only parser-owned option names and constraints may enter the error.
        names = [option for action in self._actions for option in action.option_strings
                 if option in message]
        option = max(names, key=len) if names else None
        action = self._option_string_actions.get(option) if option else None
        if "not allowed with argument" in message:
            hint = "以下参数不能同时使用：" + "、".join(sorted(set(names), key=len, reverse=True)) + "。请选择其中一种。"
        elif action is not None and action.nargs == 0:
            hint = f"{option} 是开关，不接受附加值；请去掉等号和后面的值。"
        elif action is not None and action.choices is not None:
            hint = f"{option} 须为 " + "、".join(str(value) for value in action.choices) + (" 中的一个整数。" if action.type is int else " 中的一项。")
        elif action is not None and action.type is int:
            hint = f"{option} 须为整数，例如 1024；不能放大来源声明尺寸。"
        elif option:
            hint = f"请为 {option} 提供正确的参数值；运行 --help 查看格式和示例。"
        else:
            hint = "请检查命令及完整参数名；运行 --help 查看支持的命令和示例。"
        raise _ArgumentError(hint)

    def format_help(self):
        return super().format_help().replace("usage: ", "用法：", 1)

    def _print_message(self, message, file=None):
        if message:
            stream = self.error_stream if file is sys.stderr else self.output_stream
            stream.write(message)


def _parser(stdout, stderr, command="download", *, explicit=False):
    parser = _Parser(prog="apple-artwork" + (" " + command.replace(".", " ") if explicit else ""), stdout=stdout, stderr=stderr, add_help=False,
                     description={"tasks.diff": "离线比较同一任务的两个明确版本；只读，不联网。",
                                  "tasks.check-updates": "按原任务的地区、语言、来源与选择范围检查远端声明；不下载媒体正文。"}.get(
                                      command, "发现 Apple Music 视觉素材，下载并验证图片及支持的动态封面。"),
                     allow_abbrev=not explicit,
                     epilog="查看：plan URL --json\n下载：download URL --all-media\n已有任务：tasks list；resume TASK（交互终端可省略 TASK，按编号选择）\n比较：tasks diff ID@1 ID@2；tasks check-updates TASK\n诊断：doctor；diagnostics --task-dir DIR\n自动化：capabilities --json；capabilities --guide\n同一任务在原目录自动恢复旧快照；获取新版使用 --new-version。",
                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.set_defaults(**_DEFAULTS, command=command)
    if command in {"download", "plan"}:
        parser.add_argument("url", nargs="?", help="公开的 Apple Music playlist、album、artist 或 song 链接")
    elif command == "tasks.diff":
        parser.add_argument("left_ref", metavar="LEFT", help="同一任务的完整任务ID@显式版本")
        parser.add_argument("right_ref", metavar="RIGHT", help="同一任务的另一个完整任务ID@显式版本")
    elif command in {"resume", "tasks.show", "tasks.check-updates", "doctor", "diagnostics"}:
        parser.add_argument("task_ref", nargs="?", help="资料库内的完整任务引用；或通过 --task-dir 指定原目录" +
                            ("；交互终端省略时按编号选择" if command == "resume" else ""))
        parser.set_defaults(url=None)
    else:
        parser.set_defaults(url=None)
    parser.add_argument("-h", "--help", action="help", help="显示帮助并退出")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}", help="显示版本并退出")
    allowed = _COMMAND_OPTIONS[command] | _DISPLAY_OPTIONS
    group = lambda title: _ScopedArguments(parser.add_argument_group(title), allowed)
    view = group("查看与来源")
    download = group("下载范围与质量")
    tasks = group("已有任务与目录")
    terminal = group("终端显示")
    automation = group("自动化与诊断")
    view.add_argument("--list", action="store_true", help="列出素材，不选择、不下载媒体正文、不写文件")
    view.add_argument("--response", metavar="FILE", help="从本地 JSON 读取资源信息；动态检查和下载仍可能联网")
    selectors = download.add_mutually_exclusive_group()
    selectors.add_argument("--semantic", metavar="PATH", help="选择 --list 中的精确语义路径")
    selectors.add_argument("--asset-id", metavar="ID", help="选择已发现素材的精确 ID")
    selectors.add_argument("--all-static", action="store_true", help="下载全部已识别静态呈现")
    selectors.add_argument("--all-media", action="store_true", help="下载全部图片及独立动态源；图片模板默认 PNG")
    selectors.add_argument("--all-motion", action="store_true", help="下载全部已识别的独立动态源")
    view.add_argument("--inspect-motion", action="store_true", help="与 --list 合用，检查 HLS 和 MP4 文件头，不下载媒体正文")
    view.add_argument("--compare-storefronts", metavar="CC", nargs="+", help="与 --list 合用，使用链接明确指定的 l 语言比较地区")
    download.add_argument("--motion-profile", choices=("best", "compatible"), default="best", help="best 保留最高原生档位；compatible 选择 H.264；均不转码")
    destination = tasks.add_mutually_exclusive_group()
    destination.add_argument("--output", "-o", metavar="DIR", help="精确任务目录；新目录按 images/videos 分类")
    destination.add_argument("--library", metavar="DIR", help="资料库根目录，默认 ./output，按资源及任务版本组织")
    tasks.add_argument("--task-dir", metavar="DIR", help="明确的已有任务目录")
    tasks.add_argument("--new-version", action="store_true", help="明确获取新版，保留已有版本；用于资料库下载")
    tasks.add_argument("--query", metavar="TEXT", help="按资源名称、资源 ID 或任务 ID 子串筛选，不区分大小写")
    tasks.add_argument("--type", dest="resource_type", choices=("playlist", "album", "artist", "song"), help="筛选资源类型")
    tasks.add_argument("--status", dest="statuses", action="append", default=None, metavar="STATUS", help="筛选任务状态；可重复，多个状态取并集")
    tasks.add_argument("--latest", action="store_true", help="先取每个任务最高显式版本，再应用筛选；默认列出全部版本")
    automation.add_argument("--plan", dest="plan_file", metavar="FILE", help="执行或核验 plan --json 保存的计划")
    automation.add_argument("--decisions", dest="decisions_file", metavar="FILE", help="绑定计划与决策 ID 的 JSON 选择文件")
    download.add_argument("--width", type=int, help="图片请求宽度，不放大来源声明尺寸")
    download.add_argument("--height", type=int, help="图片请求高度，不放大来源声明尺寸")
    download.add_argument("--format", choices=("jpg", "png", "webp", "avif"), help="图片 {f} 格式；--all-media 默认 PNG，其他模式默认 JPG；固定格式不变")
    view.add_argument("--provider", choices=("web", "catalog"), default="web", help="web 为实验性公开 WebPlay；catalog 使用官方开发者 token")
    view.add_argument("--token-env", default="APPLE_MUSIC_DEVELOPER_TOKEN", metavar="NAME", help="catalog 开发者 token 所在环境变量的名称")
    download.add_argument("--jobs", type=int, choices=range(1, 5), default=None, help="同时处理的文件数，默认 2，范围 1–4；仅下载模式可用")
    terminal.add_argument("--progress", choices=("auto", "always", "never"), default="auto", help="stderr 进度：auto 仅终端显示，always 强制显示，never 关闭；默认 auto")
    automation.add_argument("--json", action="store_true", help="stdout 只输出一个 JSON 结果，失败和取消也适用；下载另存 manifest.json")
    automation.add_argument("--no-input", action="store_true", help="不询问；待决状态可靠保存后返回 8")
    terminal.add_argument("--plain", action="store_true", help="使用纯文本，不刷新覆盖行或使用颜色")
    terminal.add_argument("--no-color", action="store_true", help="禁用颜色；也支持 NO_COLOR 环境变量")
    automation.add_argument("--events-jsonl", action="store_true", help="stderr 仅输出 JSONL 事件；stdout 为单个 JSON，不等待输入")
    if command == "capabilities":
        automation.add_argument("--guide", action="store_true", help="读取随包提供的 Agent 运行 Skill")
    return parser


def _error_result(error: ArtworkError) -> dict:
    result = {"schema_version": 2, "status": "failed", "errors": [error.to_dict()], "downloads": []}
    if error.code == "CANCELLED":
        result["run"] = {"cancelled": True}
    return result


def _explicit_options(parser, argv, args):
    """Record parser-owned destinations, including explicit default values."""
    result = set()
    actions = parser._option_string_actions
    for token in argv:
        if token == "--":
            break
        flag = token.partition("=")[0]
        action = actions.get(flag)
        if action is None and parser.allow_abbrev and flag.startswith("--"):
            matches = {actions[name] for name in actions if name.startswith(flag)}
            if len(matches) == 1:
                action = matches.pop()
        if action is None and flag.startswith("-") and not flag.startswith("--") and len(flag) > 2:
            action = actions.get(flag[:2])
        if action is not None:
            result.add(action.dest)
    if args.url is not None:
        result.add("url")
    if args.task_ref is not None:
        result.add("task_ref")
    return sorted(result)


def _execute(*args, **kwargs):
    from .workflow import dispatch

    return dispatch(*args, **kwargs)


def _finish_result(manifest, *, reporter, json_output, stdout, stderr, output_dir, library=None):
    # The coordinator has already drained workers. Keep a second Ctrl-C from
    # interrupting the short final flush or leaving a partial JSON document.
    previous = None
    if current_thread() is main_thread():
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        if reporter is not None:
            reporter.close()
        render_result(manifest, json_output=json_output, stdout=stdout, stderr=stderr, output_dir=output_dir,
                      show_target=not getattr(reporter, "plan_shown", False),
                      destination=getattr(reporter, "destination", None), library=library)
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


def main(argv: list[str] | None = None, *, transport: Transport | None = None,
         http: HttpClient | None = None, environ: Mapping[str, str] | None = None,
         stdout: TextIO | None = None, stderr: TextIO | None = None, stdin: TextIO | None = None,
         media_tools=None) -> int:
    """Run the same application for console scripts and injected offline tests."""
    argv = list(sys.argv[1:] if argv is None else argv)
    stdout, stderr = sys.stdout if stdout is None else stdout, sys.stderr if stderr is None else stderr
    stdin = sys.stdin if stdin is None else stdin
    environ = os.environ if environ is None else environ
    # Preserve JSON intent even if argparse rejects another argument first.
    json_output = any(value.partition("=")[0] in {"--js", "--jso", "--json", "--events-jsonl"} for value in argv)
    reporter, args = None, None
    command, explicit = "download", False
    try:
        if not argv:
            if can_interact(stdin, stdout, stderr):
                argv = TerminalInteraction(stdin, stderr).wizard()
                if argv is None:
                    stdout.write("尚未建立下载任务。可随时运行 apple-artwork 重新开始。\n")
                    return 0
            else:
                stdout.write("用法：apple-artwork plan URL --json\n      apple-artwork download URL --no-input\n\n查看帮助：apple-artwork --help\nAgent 入口：apple-artwork capabilities --guide\n")
                return 0
        if argv and argv[0] in {"download", "plan", "doctor", "tasks", "resume", "diagnostics", "capabilities"}:
            command, argv, explicit = argv[0], argv[1:], True
            if command == "tasks":
                if not argv or argv[0] not in {"list", "show", "diff", "check-updates"}:
                    raise _ArgumentError("tasks 后需接 list、show、diff 或 check-updates，例如 apple-artwork tasks list。")
                command, argv = "tasks." + argv[0], argv[1:]
        parser = _parser(stdout, stderr, command, explicit=explicit)
        args = parser.parse_args(argv)
        args.statuses = tuple(args.statuses or ())
        args.explicit_options = _explicit_options(parser, argv, args)
        json_output = args.json or args.events_jsonl
        if command in {"download", "plan"} and not args.url and not args.plan_file and not args.task_dir:
            raise _ArgumentError("请提供 Apple Music 公开链接，或使用 --plan FILE 指定已保存计划。")
        if command in {"resume", "tasks.show", "tasks.check-updates", "diagnostics"} and not args.task_ref and not args.task_dir:
            if command == "resume" and can_interact(stdin, stdout, stderr) and not (json_output or args.no_input or args.events_jsonl):
                data, bindings = query_tasks(args.library or "output")
                reference = TerminalInteraction(stdin, stderr).pick_task(data)
                if reference is None:
                    stdout.write("尚未接续任务。\n")
                    return 0
                args.task_ref = reference
                args._selection_binding = bindings[reference]
            else:
                raise _ArgumentError("请提供完整任务引用，或使用 --task-dir DIR 指定已有任务目录。")
        args.jobs_explicit = args.jobs is not None
        if args.list and args.jobs_explicit:
            raise _ArgumentError("--jobs 只用于下载，不能与 --list 同用；仅查看素材时请移除 --jobs。")
        args.jobs = 2 if args.jobs is None else args.jobs
        if command == "capabilities":
            from .commands import capabilities
            manifest, code = capabilities(args), 0
        else:
            if args.events_jsonl:
                from .commands import JsonlReporter
                reporter = JsonlReporter(stderr)
            else:
                reporter = ProgressReporter(stderr, args.progress)
                reporter.configure(plain=args.plain, no_color=args.no_color or "NO_COLOR" in environ,
                                   plan_output=stdout if not json_output and command != "plan" else None)
            provider = None
            if can_interact(stdin, stdout, stderr) and not (json_output or args.no_input or args.events_jsonl):
                interaction = TerminalInteraction(stdin, stderr, pause=reporter.pause)
                provider = interaction.decide
            manifest, code = _execute(args, http=http or HttpClient(transport), environ=environ,
                                      media_tools=media_tools, event_sink=reporter, decision_provider=provider)
    except SystemExit as error:
        return int(error.code or 0)
    except KeyboardInterrupt:
        # Normally the execution coordinator drains active work and reports
        # cancellation; this also handles an interrupt before it starts.
        error = ArtworkError("CANCELLED", "The command was cancelled.", "download")
        manifest, code = _error_result(error), 130
    except ArtworkError as error:
        manifest, code = _error_result(error), 130 if error.code == "CANCELLED" else EXIT_CODES.get(error.category, 1)
    except Exception:
        error = ArtworkError("INTERNAL_ERROR", "Unexpected internal failure; no diagnostic response data was emitted.", "internal")
        manifest, code = _error_result(error), 1
    if explicit and command != "download" and manifest.get("schema_version") == 2 and not manifest.get("target"):
        manifest = {"schema_version": 1, "kind": command, "status": manifest.get("status", "failed"),
                    "data": {}, "errors": manifest.get("errors", []), "warnings": []}
    if getattr(reporter, "failed", False):
        manifest["event_report"] = {"status": "failed", "code": "EVENT_OUTPUT_FAILED"}
        manifest.setdefault("warnings", []).append({"code": "EVENT_OUTPUT_FAILED",
                                                    "message": "The event channel could not be written; inspect the final task result."})
    _finish_result(manifest, reporter=reporter, json_output=json_output, stdout=stdout, stderr=stderr,
                   output_dir=getattr(args, "output", None) or getattr(args, "task_dir", None),
                   library=getattr(args, "library", None))
    return code
