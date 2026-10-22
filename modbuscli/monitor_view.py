import re
import shutil
import sys
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from .monitor import (
    AREA_ABBREVIATIONS,
    DATA_AREAS,
    ContinuousWriteDisplay,
    MonitorCommandKind,
    MonitorDisplayState,
    MonitorEvent,
    MonitorRange,
    MonitorWorker,
    format_value,
    parse_monitor_command,
)
from .operations import DataArea
from .terminal import paint

CHANGE_LINES = 4
MIN_FOUR_COLUMN_WIDTH = 96
RECENT_CHANGE_SECONDS = 5
_CHANGE_EVENT = re.compile(r"([A-Z]+)\[(\d+)\]")


class _CommandHistory:
    def __init__(self) -> None:
        self._commands: list[str] = []
        self._position = 0
        self._draft = ""
        self._navigating = False

    def submit(self, command: str) -> None:
        if command.strip():
            self._commands.append(command)
        self._position = len(self._commands)
        self._draft = ""
        self._navigating = False

    def previous(self, current: str) -> str:
        if not self._commands:
            return current
        if not self._navigating:
            self._draft = current
            self._position = len(self._commands)
            self._navigating = True
        self._position = max(0, self._position - 1)
        return self._commands[self._position]

    def next(self, current: str) -> str:
        if not self._navigating:
            return current
        self._position = min(len(self._commands), self._position + 1)
        if self._position == len(self._commands):
            self._navigating = False
            return self._draft
        return self._commands[self._position]


class _CommandOutputKind(Enum):
    SILENT = "silent"
    HELP = "help"
    CLEAR = "clear"
    ERROR = "error"
    EXIT = "exit"


@dataclass(frozen=True)
class _CommandResult:
    output_kind: _CommandOutputKind
    output: str = ""
    scroll_offset: int = 0


def render_monitor(
    state: MonitorDisplayState,
    ranges: dict[DataArea, MonitorRange],
    host: str,
    port: int,
    unit_id: int,
    interval: float,
    command_buffer: str,
    command_output: str = "",
    scroll_offset: int = 0,
    width: int | None = None,
    height: int | None = None,
    now: datetime | None = None,
) -> str:
    terminal = shutil.get_terminal_size(fallback=(100, 30))
    width = max(40, width or terminal.columns)
    height = max(12, height or terminal.lines)
    content_width = width - 2
    wide = width >= MIN_FOUR_COLUMN_WIDTH
    output_lines = (
        [
            line
            for output_line in command_output.splitlines()
            for line in (
                textwrap.wrap(
                    output_line,
                    width=max(20, content_width - 2),
                    replace_whitespace=False,
                    drop_whitespace=False,
                )
                or [""]
            )
        ]
        if command_output
        else []
    )
    events = state.events
    continuous_lines = _continuous_write_panel(
        state.continuous_writes, content_width
    )
    data_heading_lines = 2 if wide else 5
    data_row_lines = 1 if wide else 2
    available_data_and_events = max(
        0,
        height
        - 3
        - data_heading_lines
        - len(continuous_lines)
        - len(output_lines)
        - 1
        - 4,
    )
    event_capacity = min(
        CHANGE_LINES,
        max(0, available_data_and_events - data_row_lines),
    )
    visible_events = events[-event_capacity:] if event_capacity else ()
    event_lines = _event_panel(visible_events, content_width, event_capacity)
    now = now or datetime.now()
    recently_changed = _recently_changed(events, now)
    changes_observed = sum(event.kind == "CHANGE" for event in events)
    remaining_data_lines = max(
        0,
        available_data_and_events - event_capacity,
    )
    data_rows = remaining_data_lines // data_row_lines
    max_count = max(item.count for item in ranges.values())
    scroll_offset = min(max(0, scroll_offset), max(0, max_count - data_rows))

    lines = [
        paint(
            f"Monitor & Interact | Target: {host}:{port} | Unit ID: {unit_id}"[:width],
            "96",
            True,
        ),
        _style_header_status(
            f"{state.status} | Poll: {interval:g}s | Interactive writes: ENABLED"[:width]
        ),
        paint(f"Changes observed: {changes_observed}"[:width], "97", True),
    ]
    if wide:
        lines.extend(
            _wide_data(
                state,
                ranges,
                scroll_offset,
                data_rows,
                width,
                recently_changed,
            )
        )
    else:
        lines.extend(
            _narrow_data(
                state,
                ranges,
                scroll_offset,
                data_rows,
                width,
                recently_changed,
            )
        )

    lines.extend(event_lines)
    lines.extend(continuous_lines)
    lines.append(paint(f"monitor> {command_buffer}"[:content_width], "96", True))
    lines.extend(f"  {line}" for line in output_lines)
    return "\n".join(lines)


def _continuous_write_panel(
    writers: tuple[ContinuousWriteDisplay, ...], width: int
) -> list[str]:
    inner_width = width - 4
    lines = [
        paint(f"+{'-' * (width - 2)}+", "36", True),
        paint(f"| {'Ongoing Continuous Writes':<{inner_width}} |", "96", True),
        paint(f"+{'-' * (width - 2)}+", "36", True),
    ]
    if not writers:
        lines.append("| No continuous writes active.".ljust(width - 1) + "|")
    for writer in writers:
        end_offset = writer.offset + len(writer.values) - 1
        address = (
            f"{AREA_ABBREVIATIONS[writer.area]}[{writer.offset}]"
            if end_offset == writer.offset
            else f"{AREA_ABBREVIATIONS[writer.area]}[{writer.offset}-{end_offset}]"
        )
        value = format_value(writer.area, writer.values[0])
        cadence = (
            "as fast as possible"
            if writer.interval is None
            else f"every {writer.interval:g}s"
        )
        status = "PAUSED" if writer.paused else "RUNNING"
        control = (
            f"#{writer.writer_id} {address} = {value} | {status} | {cadence} | "
            f"{'resume' if writer.paused else 'pause'} {writer.writer_id} / "
            f"stop {writer.writer_id}"
        )
        for row in textwrap.wrap(control, width=inner_width) or [""]:
            lines.append("| " + row.ljust(inner_width) + " |")
    lines.append(paint(f"+{'-' * (width - 2)}+", "36", True))
    return lines


def _style_header_status(text: str) -> str:
    if any(status in text for status in ("ERROR", "DISCONNECTED", "FAILED")):
        color = "91"
    else:
        color = "92"
    return paint(text, color, True)


def _recently_changed(
    events: tuple[MonitorEvent, ...], now: datetime
) -> dict[tuple[DataArea, int], datetime]:
    cutoff = now - timedelta(seconds=RECENT_CHANGE_SECONDS)
    abbreviations = {
        abbreviation: area for area, abbreviation in AREA_ABBREVIATIONS.items()
    }
    recent: dict[tuple[DataArea, int], datetime] = {}
    for event in events:
        if event.kind != "CHANGE" or event.timestamp < cutoff:
            continue
        match = _CHANGE_EVENT.search(event.message)
        if match is None or match.group(1) not in abbreviations:
            continue
        key = (abbreviations[match.group(1)], int(match.group(2)))
        recent[key] = max(recent.get(key, event.timestamp), event.timestamp)
    return recent


def _wide_data(
    state: MonitorDisplayState,
    ranges: dict[DataArea, MonitorRange],
    scroll_offset: int,
    visible_rows: int,
    width: int,
    recently_changed: dict[tuple[DataArea, int], datetime],
) -> list[str]:
    column_width = (width - 6) // 4
    headers = []
    for area in DATA_AREAS:
        label = _panel_label(area, column_width)
        headers.append(paint(label.ljust(column_width), "96", True))
    lines = ["  ".join(headers)]
    headings = [paint("Offset Value".ljust(column_width), "36", True)] * 4
    lines.append("  ".join(headings))
    for index in range(visible_rows):
        row = [
            _data_cell(
                state, area, ranges[area], scroll_offset + index,
                column_width, recently_changed,
            )
            for area in DATA_AREAS
        ]
        lines.append("  ".join(row))
    return lines


def _narrow_data(
    state: MonitorDisplayState,
    ranges: dict[DataArea, MonitorRange],
    scroll_offset: int,
    visible_rows: int,
    width: int,
    recently_changed: dict[tuple[DataArea, int], datetime],
) -> list[str]:
    column_width = (width - 2) // 2
    lines: list[str] = []
    pairs = (
        (DataArea.COILS, DataArea.DISCRETE_INPUTS),
        (DataArea.INPUT_REGISTERS, DataArea.HOLDING_REGISTERS),
    )
    for pair_index, (left, right) in enumerate(pairs):
        if pair_index:
            lines.append("")
        labels = [
            paint(_panel_label(area, column_width).ljust(column_width), "96", True)
            for area in (left, right)
        ]
        lines.append("  ".join(labels))
        lines.append(
            "  ".join(
                paint("Offset Value".ljust(column_width), "36", True)
                for _ in (left, right)
            )
        )
        for index in range(visible_rows):
            row = [
                _data_cell(
                    state, area, ranges[area], scroll_offset + index,
                    column_width, recently_changed,
                )
                for area in (left, right)
            ]
            lines.append("  ".join(row))
    return lines


def _data_cell(
    state: MonitorDisplayState,
    area: DataArea,
    address_range: MonitorRange,
    index: int,
    width: int,
    recently_changed: dict[tuple[DataArea, int], datetime],
) -> str:
    if index >= address_range.count:
        return " " * width
    offset = address_range.offset + index
    values = state.snapshots.get(area)
    if area in state.errors:
        return paint(f"{offset:>5} ERROR".ljust(width), "91", True)
    if values is None or index >= len(values):
        value = "--"
    else:
        value = format_value(area, values[index])
    content = f"{offset:>5} {value}".ljust(width)
    if (area, offset) in recently_changed:
        return paint(content, "93", True)
    return content


def _event_panel(
    events: tuple[MonitorEvent, ...], width: int, max_events: int
) -> list[str]:
    lines = [
        paint(f"+{'-' * (width - 2)}+", "36", True),
        paint(f"| {'Recent Changes':<{width - 4}} |", "96", True),
        paint(f"+{'-' * (width - 2)}+", "36", True),
    ]
    visible_events = events[-max_events:] if max_events else ()
    if not visible_events and max_events > 0:
        lines.append("| No changes recorded.".ljust(width - 1) + "|")
    elif visible_events:
        for event in visible_events:
            message = event.render()[:width - 4]
            if event.kind in ("ERROR", "WRITE_FAILED", "VERIFY_FAILED", "MISMATCH"):
                rendered = paint(message.ljust(width - 4), "91", True)
            elif event.kind == "VERIFIED":
                rendered = paint(message.ljust(width - 4), "92", True)
            else:
                rendered = message
            lines.append("| " + rendered.ljust(width - 4) + " |")
    while len(lines) < max_events + 3:
        lines.append("|" + " " * (width - 2) + "|")
    lines.append(paint(f"+{'-' * (width - 2)}+", "36", True))
    return lines


def _panel_label(area: DataArea, width: int) -> str:
    label = area.menu_label
    if len(label) <= width:
        return label
    reference = {
        DataArea.COILS: "0x",
        DataArea.DISCRETE_INPUTS: "1x",
        DataArea.INPUT_REGISTERS: "3x",
        DataArea.HOLDING_REGISTERS: "4x",
    }[area]
    return f"{AREA_ABBREVIATIONS[area]} ({reference})"


def update_monitor_screen(frame: str, previous: str | None) -> str:
    lines = frame.splitlines()
    if previous is None:
        output = f"\033[2J\033[H{frame}"
    else:
        previous_lines = previous.splitlines()
        if len(previous_lines) != len(lines):
            output = f"\033[2J\033[H{frame}"
        else:
            updates = [
                (index, line)
                for index, (old_line, line) in enumerate(zip(previous_lines, lines))
                if old_line != line
            ]
            if not updates:
                return ""
            output = "\033[s"
            for index, line in updates:
                output += f"\033[{index + 1};1H\033[2K{line}"

    prompt_row = next(
        (index for index, line in enumerate(lines) if _visible_text(line).startswith("monitor>")),
        len(lines) - 1,
    )
    prompt_line = lines[prompt_row]
    prompt_column = min(
        len(_visible_text(prompt_line)) + 1,
        max(1, shutil.get_terminal_size(fallback=(100, 30)).columns),
    )
    return f"{output}\033[{prompt_row + 1};{prompt_column}H"


def _visible_text(line: str) -> str:
    return re.sub(r"\033\[[0-9;]*m", "", line)


def run_monitor(
    worker: MonitorWorker,
    read_key: Callable[[float], str],
) -> None:
    command_buffer = ""
    command_output = ""
    scroll_offset = 0
    history = _CommandHistory()
    line_mode = not sys.stdin.isatty()
    previous_frame: str | None = None
    while True:
        state = worker.state.display()
        terminal = shutil.get_terminal_size(fallback=(100, 30))
        frame = render_monitor(
            state,
            worker.config.ranges,
            worker.host,
            worker.port,
            worker.unit_id,
            worker.config.interval,
            command_buffer,
            command_output,
            scroll_offset,
            terminal.columns,
            terminal.lines,
        )
        screen_update = update_monitor_screen(frame, previous_frame)
        if screen_update:
            sys.stdout.write(screen_update)
            sys.stdout.flush()
        previous_frame = frame
        try:
            key = read_key(0.15)
        except (EOFError, KeyboardInterrupt):
            return
        if not key:
            continue
        if line_mode:
            history.submit(key.strip())
            result = _run_command(
                worker, key.strip(), scroll_offset
            )
            command_output = _apply_command_output(result, command_output)
            scroll_offset = result.scroll_offset
            if result.output_kind == _CommandOutputKind.EXIT:
                return
            continue
        if key == "\x03":
            return
        if key in ("UP", "\x1b[A", "\x1bOA"):
            command_buffer = history.previous(command_buffer)
            continue
        if key in ("DOWN", "\x1b[B", "\x1bOB"):
            command_buffer = history.next(command_buffer)
            continue
        if len(key) > 1 and ("\n" in key or "\r" in key):
            history.submit(key.strip())
            result = _run_command(
                worker, key.strip(), scroll_offset
            )
            command_output = _apply_command_output(result, command_output)
            scroll_offset = result.scroll_offset
            if result.output_kind == _CommandOutputKind.EXIT:
                return
            continue
        if key in ("\r", "\n"):
            history.submit(command_buffer)
            result = _run_command(
                worker, command_buffer, scroll_offset
            )
            command_buffer = ""
            command_output = _apply_command_output(result, command_output)
            scroll_offset = result.scroll_offset
            if result.output_kind == _CommandOutputKind.EXIT:
                return
        elif key in ("\b", "\x7f"):
            command_buffer = command_buffer[:-1]
        elif key.isprintable():
            command_buffer += key


def _apply_command_output(result: _CommandResult, current_output: str) -> str:
    if result.output_kind in (_CommandOutputKind.HELP, _CommandOutputKind.ERROR):
        return result.output
    if result.output_kind == _CommandOutputKind.CLEAR:
        return ""
    return current_output


def _run_command(
    worker: MonitorWorker, raw: str, scroll_offset: int
) -> _CommandResult:
    try:
        command = parse_monitor_command(raw)
    except ValueError as error:
        return _CommandResult(
            _CommandOutputKind.ERROR, f"Error: {error}", scroll_offset
        )

    if command.kind == MonitorCommandKind.HELP:
        return _CommandResult(
            _CommandOutputKind.HELP,
            "Commands: help, pause, resume, clear, status, quit, scroll up, scroll down. "
            "Writes: hr 0=999, c 2=on, c 1-10=on. Continuous: "
            "repeat c 1-10=on [every 0.5s]; pause 1, resume 1, or stop 1. "
            "Input registers and discrete inputs are read-only.",
            scroll_offset,
        )
    if command.kind == MonitorCommandKind.PAUSE:
        try:
            worker.pause()
        except RuntimeError as error:
            return _CommandResult(
                _CommandOutputKind.ERROR, f"Error: {error}", scroll_offset
            )
        return _CommandResult(_CommandOutputKind.SILENT, scroll_offset=scroll_offset)
    if command.kind == MonitorCommandKind.RESUME:
        try:
            worker.resume()
        except RuntimeError as error:
            return _CommandResult(
                _CommandOutputKind.ERROR, f"Error: {error}", scroll_offset
            )
        return _CommandResult(_CommandOutputKind.SILENT, scroll_offset=scroll_offset)
    if command.kind == MonitorCommandKind.CLEAR:
        return _CommandResult(_CommandOutputKind.CLEAR, scroll_offset=scroll_offset)
    if command.kind == MonitorCommandKind.STATUS:
        return _CommandResult(
            _CommandOutputKind.SILENT,
            scroll_offset=scroll_offset,
        )
    if command.kind == MonitorCommandKind.SCROLL_UP:
        return _CommandResult(
            _CommandOutputKind.SILENT,
            scroll_offset=max(0, scroll_offset - 5),
        )
    if command.kind == MonitorCommandKind.SCROLL_DOWN:
        return _CommandResult(
            _CommandOutputKind.SILENT,
            scroll_offset=scroll_offset + 5,
        )
    if command.kind == MonitorCommandKind.QUIT:
        return _CommandResult(_CommandOutputKind.EXIT, scroll_offset=scroll_offset)
    if command.kind in (
        MonitorCommandKind.PAUSE_CONTINUOUS,
        MonitorCommandKind.RESUME_CONTINUOUS,
        MonitorCommandKind.STOP_CONTINUOUS,
    ):
        assert command.writer_id is not None
        try:
            if command.kind == MonitorCommandKind.STOP_CONTINUOUS:
                worker.stop_continuous_write(command.writer_id)
            elif command.kind == MonitorCommandKind.PAUSE_CONTINUOUS:
                worker.pause_continuous_write(command.writer_id)
            else:
                worker.resume_continuous_write(command.writer_id)
        except (RuntimeError, ValueError) as error:
            return _CommandResult(
                _CommandOutputKind.ERROR, f"Error: {error}", scroll_offset
            )
        return _CommandResult(
            _CommandOutputKind.SILENT,
            scroll_offset=scroll_offset,
        )
    assert command.area is not None
    assert command.offset is not None
    assert command.value is not None
    end_offset = command.end_offset or command.offset
    values = [command.value] * (end_offset - command.offset + 1)
    if command.kind == MonitorCommandKind.START_CONTINUOUS:
        try:
            worker.start_continuous_write(
                command.area,
                command.offset,
                values,
                command.interval,
            )
        except (RuntimeError, ValueError) as error:
            return _CommandResult(
                _CommandOutputKind.ERROR, f"Error: {error}", scroll_offset
            )
        return _CommandResult(_CommandOutputKind.SILENT, scroll_offset=scroll_offset)
    try:
        worker.submit_write(command.area, command.offset, values)
    except RuntimeError as error:
        return _CommandResult(
            _CommandOutputKind.ERROR, f"Error: {error}", scroll_offset
        )
    return _CommandResult(_CommandOutputKind.SILENT, scroll_offset=scroll_offset)
