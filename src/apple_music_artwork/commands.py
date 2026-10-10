"""Local capability discovery and the explicit machine event channel."""

from __future__ import annotations

from importlib.resources import files
from io import StringIO
import json

from . import __version__


def capabilities(args):
    from .protocol import describe, envelope
    from .cli import _parser

    data = {"version": __version__, "protocol": describe(),
            "commands": ["doctor", "plan", "download", "tasks list", "tasks show", "tasks diff", "tasks check-updates", "resume", "diagnostics", "capabilities"],
            "interaction": {"requires_all_standard_streams_tty": True, "timeout_seconds": 120,
                            "task_picker_command": "resume", "task_picker_requires_reference_in_machine_mode": True,
                            "disabled_by": ["--json", "--no-input", "--events-jsonl"]},
            "output": {"stdout_json": "single_document", "events": "stderr_jsonl_opt_in"},
            "guide_command": "apple-artwork capabilities --guide"}
    data["arguments"] = {}
    for command in data["commands"]:
        parser = _parser(StringIO(), StringIO(), command.replace(" ", "."), explicit=True)
        data["arguments"][command] = [{"name": action.dest, "flags": action.option_strings,
                                       "choices": list(action.choices) if action.choices is not None else None,
                                       "default": action.default, "description": action.help}
                                      for action in parser._actions if action.dest not in {"help", "version"}]
    if args.guide:
        data["guide"] = files("apple_music_artwork").joinpath("resources/apple-artwork-operator/SKILL.md").read_text(encoding="utf-8")
    return envelope("capabilities", data)


class JsonlReporter:
    """No terminal escapes, no arbitrary fields, one LF-delimited event per line."""
    def __init__(self, stream):
        self.stream = stream
        self.closed = False
        self.failed = False

    def emit(self, event):
        if self.closed or self.failed:
            return
        from .diagnostics import event_record
        record = event_record(event)
        try:
            self.stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
            self.stream.flush()
        except OSError:
            self.failed = True

    def flush(self):
        if not self.failed:
            try:
                self.stream.flush()
            except OSError:
                self.failed = True

    def pause(self):
        self.flush()

    def close(self):
        self.closed = True

    __call__ = emit
