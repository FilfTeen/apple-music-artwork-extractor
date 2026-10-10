"""Bounded terminal input; decisions are returned to the workflow for admission."""

from __future__ import annotations

import select
import time


def can_interact(*streams):
    try:
        return all(stream.isatty() for stream in streams)
    except (AttributeError, OSError):
        return False


def _safe(value):
    return "".join(c if c.isprintable() else " " for c in str(value or ""))[:240]


class TerminalInteraction:
    def __init__(self, stdin, stderr, *, timeout=120, clock=time.monotonic,
                 wait=select.select, pause=lambda: None):
        self.stdin, self.stderr = stdin, stderr
        self.timeout, self.clock, self.wait, self.pause = timeout, clock, wait, pause

    def _read(self, deadline):
        remaining = deadline - self.clock()
        if remaining <= 0:
            return None
        try:
            ready, _, _ = self.wait([self.stdin], [], [], remaining)
            if not ready:
                return None
            line = self.stdin.readline(4097)
        except (OSError, ValueError, TypeError):
            return None
        if not line or len(line) > 4096:
            return None
        return line.strip()

    def _write(self, value):
        self.stderr.write(value)
        self.stderr.flush()

    def decide(self, context):
        """Only echo workflow-owned option labels; never evaluate input as code."""
        from .presentation import decision_lines, _for_stream

        self.pause()
        options = context.get("options", [])
        self._write("\n" + _for_stream("\n".join(decision_lines(context)), self.stderr) + "\n\n")
        for index, option in enumerate(options, 1):
            self._write(f"  {index}. {_safe(option.get('label') or option.get('action'))}\n")
        defer = next((option for option in options if option.get("action") == "defer"), None)
        self._write("\n选择编号（两分钟未选择将保存待决状态）：")
        deadline = self.clock() + self.timeout
        while True:
            value = self._read(deadline)
            if value is None:
                self._write("\n暂不选择，正在保存待决状态。\n")
                return None
            if value.isdecimal() and 1 <= int(value) <= len(options):
                option = options[int(value) - 1]
                self._write("\n")
                return {"plan_id": context["plan_id"], "decision_id": context["decision_id"],
                        "action": option["action"], "params": dict(option.get("params") or {})}
            if value.lower() in {"q", "later"} and defer:
                self._write("\n")
                return {"plan_id": context["plan_id"], "decision_id": context["decision_id"],
                        "action": "defer", "params": dict(defer.get("params") or {})}
            self._write("请输入上方有效编号；q 表示稍后处理：")

    def pick_task(self, data):
        """Numbers refer only to this read-only query; admission remains in workflow."""
        from .presentation import task_list_lines, _for_stream

        self.pause()
        self._write(_for_stream("\n".join(task_list_lines(data, numbered=True)), self.stderr) + "\n")
        choices = {str(index): row["task_ref"] for index, row in enumerate(data.get("tasks", []), 1)
                   if row.get("selectable") and row.get("task_ref")}
        if not choices:
            self._write("没有可选择的任务；未执行接续。\n")
            return None
        self._write("\n选择任务编号（q 或空行取消；两分钟未选择退出）：")
        deadline = self.clock() + self.timeout
        while True:
            value = self._read(deadline)
            if value is None or value == "" or value.casefold() == "q":
                self._write("\n未执行接续。\n")
                return None
            if value in choices:
                self._write("\n")
                return choices[value]
            self._write("请输入可选择的编号；q 取消：")

    def wizard(self):
        self._write("Apple Music 视觉素材\n\n  1. 先查看下载计划\n  2. 下载素材\n  3. 选择已有任务并接续\n  4. 检查本机能力\n\n选择编号（q 退出）：")
        deadline = self.clock() + self.timeout
        value = self._read(deadline)
        self._write("\n")
        if value in {None, "q", ""}:
            return None
        if value == "3":
            return ["resume"]
        if value == "4":
            return ["doctor"]
        if value not in {"1", "2"}:
            self._write("未选择有效任务。\n")
            return None
        self._write("\nApple Music 公开链接：")
        url = self._read(deadline)
        self._write("\n")
        if not url:
            return None
        if value == "1":
            return ["plan", url]
        self._write("\n素材范围\n  1. 主封面\n  2. 全部图片\n  3. 全部图片和动态\n\n选择编号：")
        selection = self._read(deadline)
        self._write("\n")
        if selection not in {"1", "2", "3"}:
            return None
        return ["download", url] + {"1": [], "2": ["--all-static"], "3": ["--all-media"]}[selection]
