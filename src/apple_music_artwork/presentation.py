"""Chinese terminal presentation; execution facts and JSON stay unchanged."""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import shlex
import shutil
import time
import unicodedata

from .jsonio import dumps_json


_STAGES = {
    "metadata": "获取素材信息", "inspect": "检查规格", "check_existing": "核对已有文件",
    "queued": "排队等待", "download": "下载", "validate": "验证", "save": "保存",
    "cancelled": "取消", "reused": "复用已有文件", "committed": "已保存",
    "waiting_validation": "等待验证", "failed": "失败", "skipped": "已跳过",
    "needs_action": "待处理", "deferred": "已保存，稍后处理",
}
_KINDS = {"image": "图片", "video": "动态", "unknown": "未知素材"}
_STATUS = {"success": "成功", "partial": "部分成功", "failed": "失败"}
_ERRORS = {
    "INVALID_ARGUMENTS": "参数不符合要求。", "INVALID_URL": "Apple Music 链接无效。",
    "TASK_IDENTITY_MISMATCH": "只能比较同一完整任务身份的明确版本。",
    "OFFLINE_SOURCE": "原任务来自本地响应，无法自动检查远端来源。",
    "METADATA_BODY_BLOCKED": "响应未明确声明为允许的元数据，已在首读前阻止。",
    "INVALID_JSON": "输入不是受支持的严格 JSON。", "INPUT_READ_FAILED": "无法读取输入文件。",
    "INPUT_TOO_LARGE": "输入文件超过大小限制。", "INVALID_SELECTION_OPTIONS": "素材选择参数冲突或无效。",
    "COMPARISON_LOCALE_REQUIRED": "比较地区时，链接必须明确包含 l 语言参数。",
    "INVALID_PROVIDER": "来源必须为 web 或 catalog。", "INVALID_MOTION_PROFILE": "动态档位参数无效。",
    "TOKEN_REQUIRED": "catalog 来源需要开发者 token。", "TOKEN_INVALID": "开发者 token 格式无效。",
    "TOKEN_REJECTED": "Apple 拒绝了当前凭据。", "TOKEN_DISCOVERY_FAILED": "无法取得当前公开 Web 凭据。",
    "AUTH_REDIRECT_BLOCKED": "已阻止携带凭据的跨来源重定向。",
    "API_HTTP_ERROR": "资源接口请求失败。", "API_RESPONSE_INVALID": "资源接口返回了无效 JSON。",
    "API_SCHEMA_CHANGED": "资源接口的响应结构不符合要求。", "RESOURCE_NOT_FOUND": "此地区未找到目标资源。",
    "RESOURCE_IDENTITY_CONFLICT": "响应中的资源身份互相冲突。",
    "RESOURCE_STOREFRONT_CONFLICT": "响应证明的地区与请求地区冲突，已阻止下载。",
    "TARGET_MISMATCH": "结果不属于同一资源、地区和语言。",
    "AMBIGUOUS_PRIMARY_ASSET": "同优先级存在不同来源，需要明确选择。",
    "ASSET_NOT_FOUND": "目标中没有所选素材。", "NO_VISUAL_ASSET": "未发现可选择的目标视觉素材。",
    "NO_MOTION_ASSET": "此响应中未发现已识别的动态素材。",
    "NO_PRIMARY_STATIC_ASSET": "没有符合主封面规则的图片。",
    "UNSUPPORTED_RESOURCE": "不支持此资源类型。", "UNSUPPORTED_ASSET_KIND": "不支持此素材类型。",
    "MISSING_DIMENSIONS": "图片缺少有效声明尺寸。", "INVALID_DIMENSIONS": "请求尺寸无效。",
    "DIMENSIONS_EXCEED_DECLARED": "请求尺寸超过来源声明，不能放大。",
    "INVALID_IMAGE_FORMAT": "请求图片格式不受支持。", "IMAGE_TEMPLATE_INVALID": "图片模板无效或不支持此请求。",
    "INVALID_CROP_MODE": "裁剪参数不受支持。", "UNSAFE_ASSET_URL": "图片地址不在允许的 HTTPS 来源内。",
    "UNSAFE_URL": "地址不在允许的 HTTPS 来源内。", "INVALID_REDIRECT": "重定向缺少有效目标。",
    "REDIRECT_LIMIT": "重定向次数超过限制。", "REQUEST_LIMIT": "本次真实请求预算已用尽。",
    "NETWORK_ERROR": "网络传输失败或响应不完整。", "NETWORK_TIMEOUT": "请求已超过总期限。",
    "HTTP_TRANSIENT_ERROR": "服务暂时不可用，当前自动重试已结束。",
    "DOWNLOAD_FAILED": "媒体请求失败。", "DOWNLOAD_LENGTH_MISMATCH": "媒体长度与检查记录不一致。",
    "HLS_HTTP_ERROR": "动态播放列表请求失败。", "HLS_HEAD_FAILED": "动态文件头检查失败。",
    "HLS_INVALID": "动态播放列表无效。", "HLS_METADATA_INCOMPLETE": "动态档位声明不完整。",
    "HLS_PROFILE_UNAVAILABLE": "此来源没有所选编码档位。",
    "UNSUPPORTED_HLS_LAYOUT": "动态布局不属于受支持的完整单 MP4。",
    "MOTION_DECLARATION_CONFLICT": "同一 MP4 请求的验证声明互相冲突。",
    "MOTION_DEPENDENCY_MISSING": "缺少动态解析依赖 m3u8。",
    "MEDIA_TOOL_MISSING": "缺少本机 FFmpeg 或 ffprobe。",
    "MEDIA_TOOL_UNAVAILABLE": "配置的媒体工具不可执行或身份检查失败。",
    "MEDIA_TOOL_TIMEOUT": "媒体验证超过时间限制。", "MEDIA_TOOL_OUTPUT_LIMIT": "媒体工具的诊断输出超过限制。",
    "TRANSPORT_CAPABILITY_MISSING": "当前网络适配器缺少必需功能。",
    "RESPONSE_TOO_LARGE": "响应超过允许的大小。", "IMAGE_TOO_LARGE": "图片超过解码像素限制。",
    "EMPTY_FILE": "收到的媒体为空。", "MEDIA_TYPE_MISMATCH": "实际媒体格式与请求不一致。",
    "ASSET_DIMENSION_MISMATCH": "实际解码尺寸与声明或请求不一致。",
    "ASSET_VALIDATION_FAILED": "图片未通过完整解码验证。", "VIDEO_CODEC_MISMATCH": "实际视频编码与选定档位不一致。",
    "VIDEO_FRAME_RATE_MISMATCH": "实际帧率无效或超过声明上限。",
    "VIDEO_DURATION_MISMATCH": "实际呈现跨度与完整 HLS 时间轴不一致。",
    "VIDEO_TIMELINE_INVALID": "无法证明视频时间轴连续完整。",
    "VIDEO_TRACK_MISMATCH": "媒体不满足单视频轨要求。", "VIDEO_PROBE_FAILED": "视频信息探测失败。",
    "VIDEO_DECODE_FAILED": "视频未通过完整解码。",
    "VIDEO_DECODE_TIMELINE_MISMATCH": "完整解码的帧数或时间与已核实跨度不一致。",
    "OUTPUT_CONFLICT": "输出文件或清单发生冲突，原件已保留。",
    "MANIFEST_CONFLICT": "清单不属于已登记的原任务版本，原件已保留。",
    "TASK_CONFLICT": "当前参数与此目录的原任务不同。",
    "LEGACY_MANIFEST_UNRECOGNIZED": "旧清单缺少可安全重建任务的必要信息，无法自动导入。",
    "OUTPUT_BUSY": "此输出目录正由另一任务使用。",
    "RETRY_AFTER_DEADLINE": "服务要求的等待时间超过本次操作期限，已结束此项。",
    "RUN_STATE_INVALID": "原任务状态损坏或无法识别，已保留目录。",
    "RUN_STATE_TOO_LARGE": "原任务状态超过允许的大小。",
    "RUN_STATE_WRITE_FAILED": "无法保存任务恢复状态。",
    "OUTPUT_READ_FAILED": "无法核对已有输出文件。", "OUTPUT_TOO_LARGE": "已有文件超过允许的大小。",
    "OUTPUT_IDENTITY_CHANGED": "输出对象身份已改变，已停止操作。",
    "OUTPUT_WRITE_FAILED": "无法保存输出。", "OUTPUT_CLEANUP_FAILED": "部分自有临时文件未能清理。",
    "OUTPUT_JSON_INVALID": "无法将结果保存为 JSON。", "UNSAFE_OUTPUT_PATH": "输出路径不满足安全要求。",
    "OUTPUT_PLATFORM_UNSUPPORTED": "下载需要 macOS 或 Linux 的目录描述符支持。",
    "CANCELLED": "本次任务已取消，已完成文件会保留。", "INTERNAL_ERROR": "发生内部错误，未输出原始诊断数据。",
    "TASK_REFERENCE_REQUIRED": "需要指定已有任务。", "INVALID_TASK_REFERENCE": "任务引用无效，请使用查询返回的完整引用。",
    "TASK_NOT_FOUND": "指定位置未找到已登记任务。", "LIBRARY_INVALID": "资料库的版本分配记录无法核验。",
    "NEW_OUTPUT_REQUIRED": "精确输出目录已有内容，获取新版需要明确指定新的目录。",
    "PLAN_INVALID": "计划文件无效或内容指纹不一致。", "PLAN_STALE": "目标或文件状态已变化，需要重新查看计划。",
    "DECISION_STALE": "选择对应的计划或决策已过期，请重新查看当前事项。",
    "INVALID_DECISION": "选择文件不符合当前决策允许的动作和参数。",
    "DIAGNOSTIC_STATE_INVALID": "诊断记录的身份或恢复信息无法核验。",
    "EVENT_OUTPUT_FAILED": "进度事件通道无法写入，请以最终任务结果为准。",
}
_WARNINGS = {
    "STOREFRONT_UNVERIFIED": "响应尚不能证明目标地区。",
    "PUBLIC_URL_STOREFRONT_CONFLICT": "公开页面 URL 的地区不同，不能替代接口来源证据。",
    "RESOURCE_METADATA_CONFLICT": "资源名称存在不同观察，已保留来源。",
    "VISUAL_DECLARATION_CONFLICT": "图片声明存在冲突，已保留全部观察。",
    "INVALID_VISUAL_DECLARATION": "存在无法解释的视觉声明。",
    "UNKNOWN_VISUAL_FIELD": "存在未知视觉字段，不会自动当成已知素材。",
    "INVALID_IMAGE_TRAITS": "存在无效图片修饰声明。",
    "MALFORMED_VISUAL_FIELD": "视觉字段结构异常，请检查覆盖记录。",
    "VISUAL_SCAN_LIMIT": "候选扫描达到上限，不能认定已完整发现。",
}
_UNRESOLVED = {
    "source_storefront_unverified": "来源地区未核实", "candidate_scan_incomplete": "候选扫描未完成",
    "request_coverage_unknown": "请求字段范围未知", "unknown_candidates": "存在未知候选",
    "motion_not_fully_inspected_or_unsupported": "部分动态未检查或不受支持",
    "assets_not_fully_downloaded_and_validated": "部分素材尚未下载并验证",
    "execution_errors": "存在执行错误",
    "explicitly_skipped_assets": "存在用户明确跳过的素材",
}


def _text(value, limit=180):
    return "".join(character if character.isprintable() else " " for character in str(value or ""))[:limit]


def _number(value):
    return value if type(value) in {int, float} and math.isfinite(value) and value >= 0 else None


def _bytes(value):
    if _number(value) is None:
        return "未知大小"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


def _seconds(value):
    return f"{value:g} 秒" if _number(value) is not None else "未知时长"


def _amount(current, total, formatter):
    if _number(current) is None:
        return ""
    if _number(total) is None or total <= 0:
        return formatter(current)
    ratio = f"{current / total:.1%}" if current <= total else "超过声明"
    return f"{formatter(current)} / {formatter(total)}（{ratio}）"


def _clip(text, columns):
    result, used = [], 0
    for character in text:
        width = 0 if unicodedata.combining(character) else 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
        if used + width > columns - 1:
            return "".join(result) + "…"
        result.append(character)
        used += width
    return "".join(result)


def _columns(stream, fallback=100):
    try:
        return max(20, os.get_terminal_size(stream.fileno()).columns)
    except (AttributeError, OSError, ValueError):
        return max(20, shutil.get_terminal_size(fallback=(fallback, 24)).columns)


def _wrap(text, columns):
    """Wrap by display cells without splitting away any Chinese text."""
    lines = []
    for paragraph in str(text).split("\n"):
        line, used = "", 0
        for character in paragraph:
            width = 0 if unicodedata.combining(character) else 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
            if used + width > columns and line:
                lines.append(line)
                line, used = "  ", 2
            line += character
            used += width
        lines.append(line)
    return "\n".join(lines)


def _for_stream(text, stream):
    try:
        if stream.isatty():
            columns = _columns(stream)
            # Let the terminal soft-wrap copyable commands. Inserting a newline
            # inside a quoted pathname would change the command's meaning.
            return "\n".join(line if line.startswith("apple-artwork ") else _wrap(line, columns)
                             for line in text.split("\n"))
        return text
    except (AttributeError, OSError):
        return text


def _retry_time(value):
    if _number(value) is None:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds") + "（UTC）"
    except (OverflowError, OSError, ValueError):
        return None


def _resume_command(control, *, destination=None, output_dir=None, library=None):
    resume, destination = control.get("resume") or {}, destination or {}
    reference = resume.get("task_ref")
    if reference:
        parts = ["apple-artwork", "resume", reference, "--library", destination.get("library_root") or library or "output"]
    else:
        path = destination.get("path") or output_dir
        if not path:
            return None
        parts = ["apple-artwork", "resume", "--task-dir", str(path)]
    if not all(isinstance(part, str) and part and all(char.isprintable() for char in part) for part in parts):
        return None
    return shlex.join(parts)


def _asset_name(asset):
    semantic = str(asset.get("semantic_path") or asset.get("name_key") or "")
    labels = {"artwork": "主封面", "staticDetailSquare": "方形编辑封面", "subscriptionCover": "订阅封面",
              "motionDetailSquare": "方形动态封面", "motionDetailTall": "竖版动态封面",
              "motionSquareVideo1x1": "方形动态封面", "motionTallVideo3x4": "竖版动态封面",
              "motionWideVideo21x9": "宽版动态封面", "staticDetailTall": "竖版编辑封面",
              "storeFlowcase": "展示图片", "subscriptionHero": "订阅主视觉", "superHeroTall": "竖版主视觉",
              "superHeroWide": "宽版主视觉", "emailFeature": "邮件宣传图", "bannerUber": "横幅图片",
              "centeredFullscreenBackground": "全屏背景", "originalFlowcaseBrick": "展示砖图",
              "motionArtistFullscreen16x9": "艺人全屏动态", "motionArtistSquare1x1": "艺人方形动态",
              "motionArtistWide16x9": "艺人宽版动态", "previewFrame": "动态预览帧", "primary_static": "主封面"}
    label = labels.get(asset.get("name_key")) or next((labels[part] for part in reversed(semantic.split(".")) if part in labels), None)
    return _text(label or asset.get("name") or
                 ("编辑图片" if asset.get("kind") == "image" else "动态素材"))


def decision_lines(context):
    """Workflow-supplied display facts do not authorize or select an action."""
    reason = context.get("reason")
    lines = ["需要处理", "  " + (_ERRORS.get(reason) or "此项尚未完成，请选择后续处理方式。")]
    if isinstance(reason, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", reason):
        lines.append("  错误码：" + reason)
    retry_at = _retry_time(context.get("retry_at"))
    if retry_at:
        lines.extend(["  可重试时间：", "  " + retry_at])
    items = (context.get("display") or {}).get("items", [])
    for item in items[:4]:
        spec = item.get("spec") or {}
        label = _asset_name(item)
        width, height = spec.get("width"), spec.get("height")
        detail = f" · {width}×{height}" if _number(width) is not None and _number(height) is not None else ""
        format = spec.get("codec") or spec.get("format")
        lines.append("  " + label + " · " + _KINDS.get(item.get("kind"), "素材") + detail + (" " + _text(format) if format else ""))
    if not items and context.get("item_ids"):
        lines.append(f"  涉及 {len(context['item_ids'])} 个条目；详细身份可在 tasks show 中查看。")
    return lines


class ProgressReporter:
    """One coordinator calls emit; workers never write to the terminal."""

    def __init__(self, stream, mode="auto", *, clock=time.monotonic, sleep=time.sleep, columns=None):
        if mode not in {"auto", "always", "never"}:
            raise ValueError("Invalid progress mode")
        try:
            self.tty = bool(stream.isatty())
        except (AttributeError, OSError):
            self.tty = False
        self.stream, self.clock, self.sleep = stream, clock, sleep
        self.enabled = mode == "always" or mode == "auto" and self.tty
        self.interval = 0.25 if self.tty else 1.0
        self.columns = columns
        self.last_write = -math.inf
        self.line_open = False
        self.closed = False
        self.pending = ""
        self.boundaries = []
        self.snapshot = None
        self.plain, self.no_color, self.live = False, False, None
        self.frame_lines = 0
        self.plan_output, self.plan_shown = None, False
        self.destination = None
        self.notice = ""

    def configure(self, *, plain=False, no_color=False, plan_output=None):
        self.plain, self.no_color = plain, no_color
        self.plan_output = plan_output

    def pause(self):
        if self.enabled and self.pending:
            remaining = self.interval - (self.clock() - self.last_write)
            if remaining > 0:
                self.sleep(remaining)
            self.flush()
        if self.live is not None:
            self.live.stop()
            self.live = None
        if self.line_open:
            self.stream.write("\n")
            self.stream.flush()
            self.line_open = False
        self.frame_lines = 0

    def emit(self, event):
        if isinstance(event, dict) and event.get("kind") == "plan":
            self.destination = (event.get("plan") or {}).get("destination")
            if self.plan_output is not None and not self.closed:
                self.pause()
                render_query({"schema_version": 1, "kind": "plan", "data": event.get("plan", {}),
                              "status": "success", "errors": [], "warnings": []},
                             stdout=self.plan_output, stderr=self.stream, executing=True)
                self.plan_output.flush()
                self.plan_shown = True
            return
        if not self.enabled or self.closed or not isinstance(event, dict):
            return
        if event.get("kind") == "snapshot":
            self.snapshot = event
            self.pending = "snapshot"
            self.flush()
            return
        rendered = self._render(event)
        if not rendered:
            return
        if event.get("kind") in {"retry", "cooldown"}:
            self.notice = self._render({key: value for key, value in event.items() if key not in {"active", "queued"}})
        elif event.get("kind") == "stage" and event.get("stage") in {"download", "committed", "reused", "failed", "cancelled"}:
            self.notice = ""
        self.pending = rendered
        if event.get("kind") in {"stage", "retry", "cooldown"}:
            if not self.boundaries or self.boundaries[-1] != rendered:
                self.boundaries.append(rendered)
                # Preserve recent stage transitions without building an
                # unbounded terminal queue during a slow output write.
                self.boundaries = self.boundaries[-8:]
        self.flush()

    def flush(self):
        """Called on coordinator ticks, including ticks without new events."""
        if not self.enabled or self.closed or not self.pending:
            return
        now = self.clock()
        if now - self.last_write < self.interval:
            return
        messages = list(self.boundaries)
        if not messages or messages[-1] != self.pending:
            messages.append(self.pending)
        rendered = " → ".join(messages)
        try:
            if self.snapshot is not None:
                self._write_snapshot()
            elif self.tty and not self.plain:
                previous = [message for message in self.boundaries if message != self.pending]
                current = self.pending + (" · 经过：" + " → ".join(previous) if previous else "")
                self.stream.write("\r\x1b[2K" + _clip(current, max(20, (self.columns or _columns(self.stream)) - 1)))
                self.line_open = True
            else:
                self.stream.write(rendered + "\n")
            self.stream.flush()
        except OSError:
            self.enabled = False
        self.last_write = now
        self.pending, self.boundaries = "", []

    def _write_snapshot(self):
        if self.line_open:
            self.stream.write("\r\x1b[2K" if self.tty and not self.plain else "\n")
            self.line_open = False
        state = self.snapshot
        counts = state.get("counts") or {}
        number = lambda key: counts.get(key, 0) if type(counts.get(key, 0)) is int else 0
        width = self.columns or _columns(self.stream)
        lines = ["进度", f"  已保存 {number('completed')} · 已复用 {number('reused')} · 已跳过 {number('skipped')}",
                 f"  活动 {state.get('active', 0)} · 待启动 {state.get('queued', number('pending'))} · 待处理 {state.get('needs_action', 0)} · 失败 {number('failed')}"]
        for item in state.get("items", [])[:4]:
            stage = _STAGES.get(item.get("stage"), "处理中")
            name = _asset_name(item)
            amount = _amount(item.get("received_bytes"), item.get("total_bytes"), _bytes)
            lines.append(f"  {stage} · " + _clip(name, max(10, width - 18)))
            spec = item.get("spec") or {}
            width_value, height_value = spec.get("width"), spec.get("height")
            details = _KINDS.get(item.get("kind"), "素材")
            if _number(width_value) is not None and _number(height_value) is not None:
                details += f" · {width_value}×{height_value}"
            format = spec.get("codec") or spec.get("format")
            if format:
                details += " " + _text(format)
            lines.append("    " + details)
            if amount:
                lines.append("    " + amount)
        if self.notice:
            lines.append("  " + self.notice)
        content = _wrap("\n".join(lines), max(20, width - 1))
        if self.tty and not self.plain:
            try:
                from rich.console import Console
                from rich.live import Live
                from rich.text import Text
            except ImportError:
                # Appending is safer than clearing guessed physical lines after
                # a resize; the optional UI dependency owns live rendering.
                self.stream.write(content + "\n\n")
            else:
                if self.live is None:
                    self.live = Live(console=Console(file=self.stream, force_terminal=True,
                        color_system=None if self.no_color else "auto", width=width),
                        auto_refresh=False, redirect_stdout=False, redirect_stderr=False,
                        transient=False, vertical_overflow="ellipsis")
                    self.live.start(refresh=False)
                self.live.console.width = width
                rendered = Text(content)
                rendered.highlight_words(["进度"], style="bold cyan")
                rendered.highlight_words(["已保存", "已复用"], style="green")
                rendered.highlight_words(["待处理", "等待验证"], style="yellow")
                self.live.update(rendered, refresh=True)
        else:
            self.stream.write(content + "\n\n")

    __call__ = emit

    def _render(self, event):
        kind = event.get("kind")
        if kind not in {"stage", "transfer", "retry", "cooldown", "decode"}:
            return ""
        label = _asset_name(event) if any(event.get(key) for key in ("name", "semantic_path", "filename", "item_id")) else ""
        prefix = ""
        ordinal, total = event.get("ordinal"), event.get("total")
        if type(ordinal) is int and type(total) is int and 0 < ordinal <= total:
            prefix = f"条目 {ordinal}/{total} · "
        if kind == "stage":
            message = _STAGES.get(event.get("stage"), "处理中")
        elif kind == "transfer":
            message = "下载 " + _amount(event.get("received_bytes"), event.get("expected_bytes"), _bytes)
        elif kind == "decode":
            message = "完整解码 " + _amount(event.get("processed_seconds"), event.get("total_seconds"), _seconds)
        elif kind == "retry":
            attempt = event.get("attempt")
            message = f"准备第 {attempt} 次尝试" if type(attempt) is int and attempt > 0 else "准备重试"
            message += "（整文件从头下载）"
        else:
            message = "限流等待"
        if kind in {"retry", "cooldown"} and _number(event.get("retry_after_seconds")) is not None:
            message += " · " + _seconds(event["retry_after_seconds"])
        if kind in {"transfer", "decode"} and type(event.get("attempt")) is int and event["attempt"] > 1:
            message += f" · 第 {event['attempt']} 次尝试"
        if label:
            message += " · " + label
        active, queued = event.get("active"), event.get("queued")
        if type(active) is int and type(queued) is int and active >= 0 and queued >= 0:
            message += f" · 运行 {active}，排队 {queued}"
        return prefix + message

    def close(self):
        if self.closed:
            return
        if self.enabled and self.pending:
            remaining = self.interval - (self.clock() - self.last_write)
            if remaining > 0:
                self.sleep(remaining)
            self.flush()
        if self.line_open:
            try:
                self.stream.write("\n")
                self.stream.flush()
            except OSError:
                pass
        if self.live is not None:
            self.live.stop()
            self.live = None
        self.closed = True


def _error_text(error):
    code = _text(error.get("code", "UNKNOWN_ERROR"), 80)
    message = _ERRORS.get(code, "本次操作未完成；请用 --json 查看结构化原因。")
    match = re.search(r"\bHTTP ([1-5][0-9]{2})\b", str(error.get("message", "")))
    result = f"[{code}] {message}" + (f"（HTTP {match.group(1)}）" if match else "")
    if code == "INVALID_ARGUMENTS":
        # Only the parser adds this field, using its own option definitions.
        if error.get("argument_hint"):
            result += " " + _text(error["argument_hint"], 400)
    if _number(error.get("retry_after_seconds")) is not None:
        result += " 服务要求等待 " + _seconds(error["retry_after_seconds"]) + "。"
    retry_at = _retry_time(error.get("retry_at", (error.get("context") or {}).get("retry_at")))
    if retry_at:
        result += "\n可重试时间：\n  " + retry_at + "。"
    return result


def _next_step(error):
    code, category = error.get("code", ""), error.get("category")
    if code == "CANCELLED":
        return "以相同参数和输出目录重新运行，可继续原任务；获取最新资源请使用新目录。"
    if code == "MOTION_DEPENDENCY_MISSING":
        return "在源码目录执行 python -m pip install '.[motion]'，再运行原命令。"
    if code in {"MEDIA_TOOL_MISSING", "MEDIA_TOOL_UNAVAILABLE"}:
        return "按 README 配置 FFmpeg/ffprobe，或设置 APPLE_ARTWORK_FFMPEG 与 APPLE_ARTWORK_FFPROBE 绝对路径。"
    if code == "OUTPUT_BUSY":
        return "等待占用此目录的任务结束后重试；不要删除锁文件。"
    if "CONFLICT" in code or code.startswith(("STATE_", "RESUME_", "RUN_STATE_")) or code in {"OUTPUT_IDENTITY_CHANGED", "UNSAFE_OUTPUT_PATH", "LEGACY_MANIFEST_UNRECOGNIZED"}:
        return "保留原目录和文件；使用 -o 新目录开始另一任务，不要覆盖原件。"
    if code == "OUTPUT_CLEANUP_FAILED":
        return "查看结果中列出的自有残留；已提交文件仍保留，不要据此重复覆盖。"
    if category == "auth":
        return "catalog 请检查 --token-env 指定的环境变量；Web 来源可稍后重试，不要将 token 写入命令。"
    if error.get("retryable") or code in {"NETWORK_ERROR", "NETWORK_TIMEOUT", "RETRY_AFTER_DEADLINE", "REQUEST_LIMIT", "HLS_HTTP_ERROR", "HLS_HEAD_FAILED"}:
        return "确认网络或等待提示的冷却时间后，以原参数和目录重跑；完整文件会先核验再复用。"
    if category == "input":
        return "运行 --help 检查 URL、数值及互斥参数；无须删除已有输出。"
    if category in {"discovery", "selection"}:
        return "去掉下载选择参数，用 --list --json 核对来源和素材；需要时用 --asset-id 精确选择。"
    if category == "validation":
        return "保留失败原因和已完成文件；不要降低验证要求，需重新获取最新声明时使用新目录。"
    if code in {"UNSUPPORTED_HLS_LAYOUT", "HLS_METADATA_INCOMPLETE", "HLS_PROFILE_UNAVAILABLE", "HLS_INVALID"}:
        return "用 --list --inspect-motion --json 查看所选布局；不会自动换源或降档。"
    if category == "download":
        return "检查输出目录权限和错误码；保留已完成文件后再运行原命令。"
    return "保留错误码和复现命令，使用 --json 查看详细记录。"


def _reason(reason):
    if reason in _UNRESOLVED:
        return _UNRESOLVED[reason]
    match = re.fullmatch(r"field_(.+)_(missing|malformed|mixed|unknown)", reason)
    if match:
        state = {"missing": "未返回", "malformed": "结构异常", "mixed": "观察不一致", "unknown": "状态未知"}[match[2]]
        return _text(match[1]) + " " + state
    return "其他未决项（" + _text(reason) + "）"


def _counts(manifest):
    supplied = manifest.get("run", {}).get("counts") or {}
    entries = manifest.get("downloads", [])
    fallback = {"total": len(entries), "new_downloaded": 0, "reused": 0, "failed": 0,
                "cancelled": 0, "not_started": 0, "skipped": 0, "skipped_issues": 0,
                "semantic_references": len((manifest.get("selection") or {}).get("selected", []))}
    for entry in entries:
        if entry.get("status") == "downloaded":
            fallback["reused" if entry.get("action") == "reused" else "new_downloaded"] += 1
        elif entry.get("status") == "failed":
            fallback["failed"] += 1
        elif entry.get("status") == "skipped":
            fallback["skipped"] += 1
    if supplied and "skipped" not in supplied:
        # The coordinator counts unresolved source inspections separately from
        # actual file plans; an omitted optional skipped-file count means zero.
        fallback["skipped"] = 0
    return {name: supplied[name] if type(supplied.get(name)) is int and supplied[name] >= 0 else value
            for name, value in fallback.items()}


def render_result(manifest, *, json_output, stdout, stderr, output_dir=None, show_target=True,
                  destination=None, library=None):
    """Render only reported facts; a progress event never marks an item successful."""
    if json_output:
        stdout.write(dumps_json(manifest))
        return
    if manifest.get("schema_version") == 1:
        return render_query(manifest, stdout=stdout, stderr=stderr, destination=destination,
                            output_dir=output_dir, library=library)
    output_dir = output_dir or (destination or {}).get("path")
    lines, diagnostics = [], []
    control = manifest.get("control") or {}
    guided = bool(control.get("decision_required") or control.get("lifecycle") in {"deferred", "cancelled"})
    target = manifest.get("target") or {}
    key = target.get("key") or {}
    if key and show_target:
        name = _text(manifest.get("resource_name") or key.get("resource_id"))
        lines.extend(["目标", f"  {name} · 地区 {_text(key.get('storefront'))} · 语言 {_text(target.get('locale') or '未指定')}", ""])
    if manifest.get("mode") == "list":
        assets = manifest.get("assets", [])
        lines.append("可用素材")
        for asset in assets:
            width, height = asset.get("width"), asset.get("height")
            size = f"声明 {width}×{height}" if width and height else "规格尚未检查" if asset.get("kind") == "video" else "声明尺寸未知"
            lines.append(f"  {_asset_name(asset)} · {_KINDS.get(asset.get('kind'), '未知素材')} · {size}")
        lines.append(f"发现 {len(assets)} 个目标语义引用；未下载媒体正文。")
        for item in manifest.get("motion_inspections", []):
            inspection = item.get("inspection") or {}
            selected, layout = inspection.get("selected") or {}, inspection.get("layout") or {}
            label = _text(item.get("semantic_path") or item.get("asset_id"))
            if item.get("status") == "supported":
                fps = selected.get("frame_rate")
                details = f"{selected.get('width')}×{selected.get('height')} {_text(selected.get('codec') or selected.get('codecs'))}"
                if _number(fps) is not None:
                    details += f" · {fps:g} fps"
                details += " · " + _seconds(layout.get("duration")) + " · " + _bytes(layout.get("content_length"))
                lines.append(f"动态检查：{label} · {details}（声明规格，尚未完整解码）")
            else:
                lines.append(f"动态检查：{label} · {'布局不受支持' if item.get('status') == 'unsupported' else '检查失败'}")
        for compared in manifest.get("comparison", {}).get("results", [])[1:]:
            storefront = compared.get("target", {}).get("key", {}).get("storefront")
            lines.append(f"比较地区 {_text(storefront)}：{_STATUS.get(compared.get('status'), '未知结果')}，{len(compared.get('assets', []))} 个语义引用。")
    else:
        lines.append("结果")
        for item in manifest.get("downloads", []):
            if item.get("status") == "skipped":
                lines.append(f"已跳过 {_asset_name(item)} · {_KINDS.get(item.get('kind'), '素材')}（未完成，不计为已保存）")
                continue
            if item.get("status") != "downloaded":
                continue
            validation = item.get("validation") or {}
            dimensions = validation.get("actual_dimensions") or {}
            kind = item.get("kind") or ("video" if validation.get("actual_format") == "MP4" else "image")
            name = _asset_name({**item, "kind": kind}) + " · " + _KINDS.get(kind, "素材")
            size = (f"{dimensions['width']}×{dimensions['height']}" if dimensions.get("width") and dimensions.get("height") else "尺寸未记录")
            size += " " + _text(validation.get("actual_format"))
            if validation.get("actual_codec"):
                size += " " + _text(validation["actual_codec"])
            if _number(validation.get("duration_seconds")) is not None:
                size += " · " + _seconds(validation["duration_seconds"])
            if item.get("action") == "reused":
                checked = "本次完整本地解码验证通过" if item.get("local_verification", {}).get("decoded_this_run") is True else "本次进行本地核验"
                lines.append(f"已复用 {name} · {size}（保留原解码验证；{checked}）")
            else:
                lines.append(f"已保存 {name} · 实际 {size}（解码验证通过）")
        counts = _counts(manifest)
        skipped = f"，已跳过 {counts['skipped']}" if control or counts["skipped"] else ""
        lines.append(f"本次文件任务：新下载 {counts['new_downloaded']}，复用 {counts['reused']}，失败 {counts['failed']}，取消 {counts['cancelled']}，未执行 {counts['not_started']}{skipped}；共 {counts['total']} 个文件、{counts['semantic_references']} 个语义引用。")
        if counts["skipped_issues"]:
            lines.append(f"另有 {counts['skipped_issues']} 个尚未形成文件计划的素材来源已明确跳过。")
    run = manifest.get("run") or {}
    status = "已取消" if run.get("cancelled") else _STATUS.get(manifest.get("status"), "未完成")
    lines.append("本次命令：" + status + "。")
    if control.get("lifecycle") == "deferred":
        lines.extend(["", "下一步", "  已保存稍后处理状态；到允许的重试时间后可使用 resume 继续。"])
    elif control.get("decision_required"):
        lines.extend(["", "下一步", "  待处理事项已保存。使用 resume 继续，并明确选择处理方式。"])
    if control.get("decision_required") or control.get("lifecycle") in {"deferred", "cancelled"}:
        command = _resume_command(control, destination=destination, output_dir=output_dir, library=library)
        if command:
            lines.append(command)
    if run.get("resumed"):
        created = run.get("snapshot", {}).get("created_at")
        pending = run.get("pending_inspection") or {}
        resumed = f"继续原资源快照（采集时间：{_text(created or '未知')}）"
        if type(pending.get("attempted")) is int and pending["attempted"] > 0:
            completed = pending.get("completed", 0)
            resumed += f"；补查上次未完成的动态规格，完成 {completed}/{pending['attempted']} 项"
            if pending.get("checked_at"):
                resumed += f"（检查时间：{_text(pending['checked_at'])}）"
        lines.append(resumed + "；资源目录信息未刷新，" + ("获取新版请用 download --new-version 明确开始。" if control else "获取最新素材请使用新目录。"))
    stats = run.get("stats") or {}
    if type(stats.get("requests")) is int:
        lines.append(f"本次实际请求 {stats['requests']} 次，收到网络字节 {_bytes(stats.get('received_bytes'))}；本地复用不计入传输量。")
    if run.get("residue"):
        for path in run["residue"]:
            lines.append("保留残留（未自动清理）：" + _text(path, 400))
    completeness = manifest.get("coverage", {}).get("completeness")
    if completeness:
        if completeness.get("status") == "complete":
            lines.append("资源完整程度：在此资源、地区、语言、响应及支持布局范围内完整。")
        else:
            reasons = "；".join(_reason(reason) for reason in completeness.get("unresolved", []))
            lines.append("资源完整程度：仍有未决项" + ("（" + reasons + "）" if reasons else "") + "。")
    report = manifest.get("manifest_report")
    if report:
        path = report.get("path", "manifest.json")
        if output_dir is None and not control and key.get("resource_id"):
            output_dir = Path("output") / key["resource_id"]
        location = str(Path(output_dir) / path) if output_dir is not None else path
        if control and output_dir is not None:
            lines.append("输出位置：" + _text(output_dir, 4096))
            location = path
        saved = {"saved": "已保存", "failed": "保存未完成", "not_saved": "本次未保存"}.get(report.get("status"), "状态未知")
        if report.get("status") == "failed" and report.get("committed") is True:
            saved = "已提交，但收尾失败"
        lines.append(f"清单：{saved} · {_text(location, 400)}")
    reported = set()
    for item in manifest.get("downloads", []):
        error = item.get("error") or item.get("cleanup_error")
        if not error:
            continue
        reported.add((error.get("code"), error.get("message")))
        if item.get("status") == "skipped":
            continue
        label = _asset_name(item) if control else _text(item.get("semantic_path") or item.get("asset_id") or item.get("path"))
        diagnostics.append(f"素材 {label}：{_error_text(error)}" + ("" if guided else "\n下一步：" + _next_step(error)))
    for item in manifest.get("motion_inspections", []):
        error = item.get("error")
        if error and item.get("status") == "skipped":
            reported.add((error.get("code"), error.get("message")))
            continue
        if error and (error.get("code"), error.get("message")) not in reported:
            reported.add((error.get("code"), error.get("message")))
            label = _asset_name(item) if control else _text(item.get('semantic_path') or item.get('asset_id'))
            diagnostics.append(f"动态 {label}：{_error_text(error)}" + ("" if guided else "\n下一步：" + _next_step(error)))
    for error in manifest.get("errors", []):
        if (error.get("code"), error.get("message")) not in reported:
            reported.add((error.get("code"), error.get("message")))
            diagnostics.append(_error_text(error) + ("" if guided else "\n下一步：" + _next_step(error)))
    for warning in manifest.get("warnings", []):
        code = _text(warning.get("code"), 80)
        description = _WARNINGS.get(code) or _ERRORS.get(code) or "存在需核对的来源信息，请使用 --json 查看记录。"
        diagnostics.append(f"提示 [{code}]：{description}")
    stdout.write(_for_stream("\n".join(lines), stdout) + "\n")
    if diagnostics:
        stderr.write(_for_stream("\n待核对事项\n\n" + "\n\n".join(diagnostics), stderr) + "\n")


def task_list_lines(data, *, numbered=False):
    """Use the same resource facts for listing and the bounded task picker."""
    lines = ["已有任务"]
    tasks, errors = data.get("tasks", []), data.get("errors", [])
    states = {"allocated": "等待开始", "running": "进行中", "needs_action": "等待选择",
              "deferred": "已稍后处理", "completed": "已完成", "cancelled": "已取消", "failed": "失败"}
    for index, task in enumerate(tasks, 1):
        prefix = f"  {index}. " if numbered else "  "
        selection = task.get("selection") or {}
        scope = _text(task.get("selection_label") or "未知")
        if selection.get("semantic_path") or selection.get("asset_id"):
            scope += " · " + _text(selection.get("semantic_path") or selection["asset_id"])
        lines.append(prefix + _text(task.get("resource_name") or task.get("resource_id") or "未知资源") +
                     " · " + states.get(task.get("lifecycle"), "待检查") +
                     (" · 不可选择" if not task.get("selectable") else "") +
                     (" · 需要恢复元数据" if task.get("recovery_required") else ""))
        lines.extend(["    类型：" + _text(task.get("resource_type") or "未知") +
                      " · 地区／语言：" + _text(task.get("storefront") or "未知") + "／" + _text(task.get("locale") or "未知"),
                      "    范围：" + scope,
                      "    快照：" + _text(task.get("snapshot_created_at") or "未知"),
                      "    任务：" + _text(task.get("task_ref") or task.get("task_id"))])
        if task.get("error"):
            lines.append("    " + _error_text(task["error"]))
    for item in errors:
        lines.append("  无法读取登记：" + _text(item.get("relative_path") or "未知位置"))
        lines.append("    " + _error_text(item.get("error") or {}))
    if not tasks and not errors:
        lines.append("  指定资料库内暂无已登记任务。")
    elif not tasks:
        lines.append("  没有可显示的任务；以上登记存在错误。")
    return lines


def _comparison_fact(fact):
    if not fact.get("available"):
        return "未能观察"
    if not fact.get("present"):
        return "记录缺失"
    value = fact.get("value")
    if value is None:
        return "null（空值）"
    if isinstance(value, list) and not value:
        return "空数组 []"
    if isinstance(value, dict) and not value:
        return "空对象 {}"
    if value == "":
        return '空字符串 ""'
    text = " ".join(dumps_json(value).splitlines())
    return _text(text, 280) + (" …（完整值见 --json）" if len(text) > 280 else "")


_COMPARISON_LIMITATIONS = {
    "metadata_only": "地址变化不证明内容变化；声明未变化不证明媒体字节相同。",
    "comparison_scope": "字段状态覆盖目标响应；素材与动态只比较原选择范围，排除关联素材。",
    "declared_metadata_boundary": "已声明或识别为媒体的响应在首读前阻止；服务器将媒体谎报为允许的文本仍无法排除。",
    "field_observation_unavailable": "字段状态观察不可用，不能判断缺席。",
    "discovery_incomplete": "扫描完成证据缺失、未知或不完整，不能确认素材新增或消失。",
    "selection_observation_unavailable": "原选择或本次选择观察不可用。",
    "asset_observation_incomplete": "部分素材声明未能完整观察。",
    "motion_observation_incomplete": "动态规格或布局尚未完整核实。",
    "offline_source": "原任务来自离线响应，无法检查远端；不会自动切换来源。",
    "resource_observation_failed": "资源信息请求失败，不能解释为素材删除。",
    "motion_observation_failed": "动态检查请求失败，已观察到的变化仍保留。",
    "selection_unresolved": "原选择无法明确对应本次观察，不自动替换来源。",
    "source_evidence_conflict": "来源证据存在冲突，无法判断。",
    "exact_selection_unavailable": "原精确素材 ID 无法匹配；语义差异仅供观察，不检查替代动态源。",
    "primary_selection_changed": "主封面规则已指向另一语义来源，不自动跟随替代来源。",
    "selection_mapping_ambiguous": "选择存在缺席或多个候选，无法唯一对应。",
}


def _comparison_semantic(key):
    if not key:
        return "目标字段"
    owner = key.get("owner") or {}
    return _text(key.get("semantic_path") or "未知语义") + (
        " · 资源 " + _text(owner["resource_id"]) if owner.get("resource_id") else "")


def comparison_lines(data, *, remote=False):
    outcomes = {"observed_change": "观察到变化", "no_observed_change": "未观察到变化", "indeterminate": "无法判断"}
    lines = ["远端更新检查" if remote else "任务版本比较", "  结论：" + outcomes.get(data.get("outcome"), "无法判断")]
    if data.get("task_id"):
        lines.append("  任务 ID：" + _text(data["task_id"]))
    for is_local, label, side in ((True, "原快照", data.get("left") or {}),
                                 (not remote, "远端观察" if remote else "对照快照", data.get("right") or {})):
        lines.append("  " + label)
        if is_local:
            lines.append("    任务引用：" + _text(side.get("task_ref") or "未知"))
            version = side.get("version")
            lines.append("    版本：" + (str(version) if version is not None else "未知"))
        lines.append("    时间：" + _text(side.get("snapshot_created_at") or "未知"))
    lines.extend(["", "已观察的差异与证据"])
    kinds = {"field_state": "字段状态", "asset": "素材声明", "motion": "动态声明"}
    for change in data.get("changes", []):
        field = change.get("field")
        label = "地址变化（url）" if field == "url" else _text(field)
        lines.extend(["  " + kinds.get(change.get("kind"), "观察") + " · " + _comparison_semantic(change.get("semantic_key")) + " · " + label,
                      "    原：" + _comparison_fact(change["before"]), "    对照：" + _comparison_fact(change["after"])])
    if not data.get("changes"):
        lines.append("  未记录可比较的差异；请结合结论和限制阅读。")
    for item in data.get("ambiguities", []):
        lines.extend(["", "歧义：" + _comparison_semantic(item.get("semantic_key")),
                      "  无法唯一对应 · 原候选 " + _text(str(item.get("left_count", "未知"))) +
                      " · 对照候选 " + _text(str(item.get("right_count", "未知"))) + " [" + _text(item.get("reason")) + "]"])
    lines.extend(["", "结论范围与限制"])
    for item in data.get("limitations", []):
        reason = item.get("reason")
        text = _COMPARISON_LIMITATIONS.get(reason, "尚有未解释的限制；完整证据见 --json。")
        lines.append("  " + text + " [" + _text(reason) + "]" +
                     (" · " + _comparison_semantic(item["semantic_key"]) if item.get("semantic_key") else "") +
                     (" · [" + _text(item["code"]) + "]" if item.get("code") else ""))
    scope = data.get("network_scope") or {}
    observed = [label for key, label in (("resource_metadata", "资源信息"), ("hls_and_head", "HLS／HEAD")) if scope.get(key)]
    media = "有" if scope.get("media_bodies") else "无" if scope.get("media_bodies") is False else "未知"
    requests = "、".join(observed) if observed else "无" if all(
        key in scope for key in ("resource_metadata", "hls_and_head")) else "未知"
    lines.extend(["", "本次实际请求：" + requests + "；媒体正文请求：" + media + "。",
                  "完整结构、字段值与证据请使用 --json 查看。"])
    return lines


def render_query(result, *, stdout, stderr, executing=False, destination=None, output_dir=None, library=None):
    data = result.get("data") or {}
    kind = result.get("kind")
    if kind == "capabilities" and data.get("guide"):
        stdout.write(data["guide"] + ("" if data["guide"].endswith("\n") else "\n"))
        return
    lines = []
    if kind == "plan":
        target = data.get("target") or {}
        key = target.get("key") or {}
        resource_name = data.get("snapshot", {}).get("manifest", {}).get("resource_name")
        lines.extend(["目标", "  " + _text(resource_name or data.get("resource_name") or key.get("resource_id") or "所选资源"),
                      "  地区 " + _text(key.get("storefront") or "未知") + " · 语言 " + _text(target.get("locale") or "未指定"), "", "预检"])
        checks = data.get("checks") or data.get("local_observations") or {}
        if isinstance(checks, dict):
            lines.append("  " + ("已有任务目录，将核对文件" if checks.get("exists") else "新任务目录"))
            if checks.get("busy"):
                lines.append("  目录正在使用，须等待当前任务结束")
        else:
            for check in checks:
                if check.get("id") == "space":
                    lines.append("  可用空间 " + _bytes(check.get("available_bytes")) + " · 已知待下载 " + _bytes(check.get("known_required_bytes")))
                    if check.get("unknown_size_count"):
                        lines.append(f"  {check['unknown_size_count']} 个条目的大小未知，空间需求仅为预估")
                elif check.get("id") == "permissions":
                    lines.append("  写入权限预估：" + ("可写" if check.get("writable") else "不可写"))
                elif check.get("id") == "output_busy":
                    lines.append("  目录占用：" + ("正在使用" if check.get("status") == "blocked" else "当前可用"))
                else:
                    labels = {"motion_parser": "动态解析依赖", "media_tools": "媒体验证工具", "motion_tools": "媒体验证工具", "pillow": "图像解码依赖"}
                    states = {"ok": "可用", "passed": "通过", "missing": "缺失", "blocked": "受阻", "estimated": "预估"}
                    lines.append("  " + labels.get(check.get("id"), "必要条件") + "：" + states.get(check.get("status"), "待核对"))
        scope = data.get("network_scope") or {}
        requested = [label for key, label in (("resource_metadata", "资源信息"), ("hls_and_head", "HLS 与文件头")) if scope.get(key)]
        lines.append("  本次预检联网：" + ("、".join(requested) if requested else "不需要") + "；不下载媒体正文")
        destination = data.get("destination") or {}
        if destination.get("path"):
            lines.append("  输出位置：" + _text(destination["path"], 400))
        lines.extend(["", "执行计划"])
        actions = {"download": "下载", "reuse": "复用", "reused": "复用", "skip": "跳过", "wait": "待处理",
                   "needs_action": "待处理", "validate_local": "本地验证"}
        files = {item.get("item_id"): item for item in data.get("snapshot", {}).get("files", [])}
        for item in data.get("items", []):
            file = files.get(item.get("item_id"), {})
            name = _asset_name(file.get("asset") or item)
            spec = item.get("spec") or {}
            request = file.get("request") or {}
            width, height = spec.get("width") or request.get("width"), spec.get("height") or request.get("height")
            details = f" · {width}×{height}" if width and height else ""
            format = spec.get("codec") or spec.get("format") or request.get("format")
            if format:
                details += " " + _text(format)
            lines.append(f"  {actions.get(item.get('action'), '待检查')} · {name} · {_KINDS.get(item.get('kind'), '素材')}" + details)
        if not data.get("items"):
            lines.append("  尚无可执行条目；请核对预检或待决原因。")
        for decision in data.get("decisions", []):
            lines.extend(["", *decision_lines(decision)])
        if not executing:
            lines.extend(["", "下一步", "  使用 download 执行；自动化可保存 plan --json 的结果并通过 --plan 与 --decisions 提交选择。"])
        else:
            lines.append("")
    elif kind in {"tasks.diff", "tasks.check-updates"}:
        lines.extend(comparison_lines(data, remote=kind == "tasks.check-updates"))
    elif kind == "doctor":
        lines.append("本机能力检查")
        labels = {"python": "Python", "pillow": "图像解码", "output_platform": "安全文件操作",
                  "motion_parser": "动态解析", "terminal_ui": "终端界面", "media_tools": "FFmpeg / ffprobe"}
        states = {"ok": "可用", "missing": "未安装", "unsupported": "不支持", "failed": "检查失败"}
        for item in data.get("checks", []):
            lines.append(f"  {labels.get(item.get('id'), '必要条件')}：{states.get(item.get('status'), '待核对')}" +
                         (" · " + _text(item["version"]) if item.get("version") else "") +
                         (f" [{_text(item['code'])}]" if item.get("code") else ""))
        lines.extend(["", "本检查不下载媒体，也不证明视频已完整解码。"])
    elif kind == "tasks.list":
        lines.extend(task_list_lines(data))
        if data.get("tasks"):
            lines.extend(["", "下一步", "  使用 tasks show TASK 查看详情，或 resume TASK 接续；指定资料库时继续带上 --library。"])
    elif kind == "tasks.show":
        snapshot, control = data.get("snapshot") or {}, data.get("control") or {}
        manifest = snapshot.get("manifest") or {}
        states = {"allocated": "等待开始", "running": "进行中", "needs_action": "等待选择",
                  "deferred": "已稍后处理", "completed": "已完成", "cancelled": "已取消", "failed": "失败"}
        lines.extend(["任务详情", "  " + _text(manifest.get("resource_name") or "所选资源"),
                      "  状态：" + states.get(control.get("lifecycle"), "待检查"),
                      "  任务：" + _text(data.get("task_id")), "", "文件"])
        files = data.get("items") or {}
        for item in snapshot.get("files", []):
            state = files.get(item.get("item_id"), {})
            status = {"committed": "已保存", "failed": "待处理", "planned": "待开始", "pending": "待开始",
                      "cancelled": "已取消", "skipped": "已跳过"}.get(state.get("status"), "待检查")
            lines.append("  " + _asset_name(item.get("asset") or item) + " · " + status)
        for context in control.get("decisions", []):
            lines.extend(["", *decision_lines(context)])
        command = _resume_command(control, destination=destination, output_dir=output_dir, library=library)
        lines.extend(["", "下一步", "  以下命令继续原任务：" if command else "  使用 resume 和原任务目录或完整任务引用继续。"])
        if command:
            lines.append(command)
        lines.append("  自动化可加 --json 查看完整计划、待决选项与身份。")
    else:
        titles = {"capabilities": "自动化能力", "tasks.show": "任务详情", "diagnostics": "诊断记录", "resume": "接续结果"}
        lines.extend([titles.get(kind, "结果"), dumps_json(data).rstrip()])
    stdout.write(_for_stream("\n".join(lines), stdout) + "\n")
    errors = result.get("errors", [])
    if errors:
        stderr.write(_for_stream("\n待核对事项\n\n" + "\n\n".join(_error_text(error) + "\n下一步：" + _next_step(error) for error in errors), stderr) + "\n")
