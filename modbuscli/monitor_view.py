import shutil
import sys
import textwrap
from collections.abc import Callable

from .monitor import (
    AREA_ABBREVIATIONS,
    DATA_AREAS,
    MonitorCommandKind,
    MonitorDisplayState,
    MonitorRange,
    MonitorWorker,
    format_value,
    parse_monitor_command,
)
from .operations import DataArea
from .terminal import paint

CHANGE_LINES = 4
MIN_FOUR_COLUMN_WIDTH = 96


def render_monitor(
    state: MonitorDisplayState,
    ranges: dict[DataArea, MonitorRange],
    host: str,
    port: int,
    unit_id: int,
    interval: float,
    command_buffer: str,
    notice: str = "",
    scroll_offset: int = 0,
    width: int | None = None,
    height: int | None = None,
) -> str:
    terminal = shutil.get_terminal_size(fallback=(100, 30))
    width = max(40, width or terminal.columns)
    height = max(12, height or terminal.lines)
    content_width = width - 6
    wide = width >= MIN_FOUR_COLUMN_WIDTH
    panel_rows = 1 if wide else 2
    panel_headers = 2
    notices = (
        textwrap.wrap(notice.replace("\n", " "), width=max(20, content_width))
        if notice
        else []
    )
    events = state.events[-CHANGE_LINES:]
    placeholder_events = not events
    body_capacity = height - 3
    fixed_body_lines = 3 + len(notices) + panel_headers + 1
    available_events = max(
        0, body_capacity - fixed_body_lines - panel_rows
    )
    visible_events = events[-available_events:] if available_events else ()
    event_line_count = len(visible_events) or int(placeholder_events)
    visible_rows = max(
        1,
        (body_capacity - fixed_body_lines - event_line_count) // panel_rows,
    )
    max_count = max(item.count for item in ranges.values())
    scroll_offset = min(max(0, scroll_offset), max(0, max_count - visible_rows))

    lines = [
        f"Monitor & Interact | {host}:{port} | Unit ID {unit_id}",
        f"{state.status} | Poll interval: {interval:g}s | Interactive writes: ENABLED",
        "C/DI/IR/HR = 0x/1x/3x/4x | zero-based | * changed | help | scroll up/down",
    ]
    lines.extend(notices)

    if wide:
        cell_width = (content_width - 6) // 4
        lines.extend(_wide_panels(state, ranges, scroll_offset, visible_rows, cell_width))
    else:
        cell_width = (content_width - 2) // 2
        lines.extend(
            _narrow_panels(state, ranges, scroll_offset, visible_rows, cell_width)
        )

    lines.append("Changes (chronological):")
    if visible_events:
        lines.extend(event.render() for event in visible_events)
    else:
        if placeholder_events:
            lines.append("No changes recorded.")

    prompt = f"monitor> {command_buffer}"
    if len(lines) > body_capacity:
        lines = lines[-body_capacity:]
    lines.extend([""] * max(0, body_capacity - len(lines)))
    lines.append(prompt[:content_width])
    return _box_and_style(lines, width, content_width)


def _box_and_style(lines: list[str], width: int, content_width: int) -> str:
    border = "+" + "-" * (width - 2) + "+"
    styled = [paint(border, "31", bold=True)]
    for index, line in enumerate(lines):
        content = line[:content_width].ljust(content_width)
        row = f"|  {content}  |"
        if index == 0:
            row = _style_row(row, "91", True)
        elif index == 1:
            row = _style_status(row)
        elif index == 2:
            row = _style_row(row, "36")
        elif content.startswith("Offset"):
            row = _style_row(row, "93", True)
        elif " (" in content and content.strip().endswith(("0x)", "1x)", "3x)", "4x)")):
            row = _style_row(row, "96", True)
        elif content.startswith("Changes"):
            row = _style_row(row, "93", True)
        elif content.startswith("monitor>"):
            row = _style_row(row, "96", True)
        elif any(
            marker in content
            for marker in (" WRITE ", " VERIFIED ", " MISMATCH ", " WRITE FAILED ")
        ):
            row = _style_event(row, content)
        elif content.strip().startswith("ERROR") or "read failed:" in content:
            row = _style_row(row, "91", True)
        elif "*" in content:
            row = _style_row(row, "93", True)
        elif "  ON" in content:
            row = _style_row(row, "92")
        elif "  OFF" in content:
            row = _style_row(row, "90")
        styled.append(row)
    styled.append(paint(border, "31", bold=True))
    return "\n".join(styled)


def _style_row(row: str, color: str, bold: bool = False) -> str:
    return f"|  {paint(row[3:-3], color, bold)}  |"


def _style_status(row: str) -> str:
    status_color = "92" if "RUNNING" in row else "93" if "PAUSED" in row else "91"
    value = row[3:-3]
    value = value.replace("RUNNING", paint("RUNNING", status_color, True))
    value = value.replace("PAUSED", paint("PAUSED", status_color, True))
    value = value.replace(
        "Interactive writes: ENABLED", paint("Interactive writes: ENABLED", "92", True)
    )
    return f"|  {value}  |"


def _style_event(row: str, content: str) -> str:
    event = content.strip()
    color = (
        "92"
        if "VERIFIED " in event
        else "91"
        if "MISMATCH " in event or "WRITE FAILED " in event
        else "93"
    )
    return _style_row(row, color, True)


def _wide_panels(
    state: MonitorDisplayState,
    ranges: dict[DataArea, MonitorRange],
    scroll_offset: int,
    visible_rows: int,
    cell_width: int,
) -> list[str]:
    lines = [
        "  ".join(
            _fit(_panel_label(area, cell_width), cell_width)
            for area in DATA_AREAS
        ),
        "  ".join(_fit("Offset  Value", cell_width) for _ in DATA_AREAS),
    ]
    for index in range(scroll_offset, scroll_offset + visible_rows):
        cells = [
            _render_cell(state, area, ranges[area], index, cell_width)
            for area in DATA_AREAS
        ]
        lines.append("  ".join(cells))
    return lines


def _narrow_panels(
    state: MonitorDisplayState,
    ranges: dict[DataArea, MonitorRange],
    scroll_offset: int,
    visible_rows: int,
    cell_width: int,
) -> list[str]:
    lines: list[str] = []
    pairs = (
        (DataArea.COILS, DataArea.DISCRETE_INPUTS),
        (DataArea.INPUT_REGISTERS, DataArea.HOLDING_REGISTERS),
    )
    for left, right in pairs:
        lines.append(
            _fit(_panel_label(left, cell_width), cell_width)
            + "  "
            + _fit(_panel_label(right, cell_width), cell_width)
        )
        for index in range(scroll_offset, scroll_offset + visible_rows):
            lines.append(
                _render_cell(state, left, ranges[left], index, cell_width)
                + "  "
                + _render_cell(state, right, ranges[right], index, cell_width)
            )
    return lines


def _render_cell(
    state: MonitorDisplayState,
    area: DataArea,
    address_range: MonitorRange,
    index: int,
    cell_width: int,
) -> str:
    if index >= address_range.count:
        return " " * cell_width
    address = address_range.offset + index
    values = state.snapshots.get(area)
    if area in state.errors:
        value = f"ERR:{state.errors[area]}"
    elif values is None or index >= len(values):
        value = "--"
    else:
        value = format_value(area, values[index])
    changed = (area, address) in state.changed
    marker = "*" if changed else " "
    return _fit(f"{address:>5} {marker}{value}", cell_width)


def _fit(value: str, width: int) -> str:
    return value[:width].ljust(width)


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


def run_monitor(
    worker: MonitorWorker,
    read_key: Callable[[float], str],
) -> None:
    command_buffer = ""
    notice = ""
    scroll_offset = 0
    line_mode = not sys.stdin.isatty()
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
            notice,
            scroll_offset,
            terminal.columns,
            terminal.lines,
        )
        print(f"\033[2J\033[H{frame}", end="", flush=True)
        key = read_key(0.15)
        if not key:
            continue
        if line_mode:
            notice, should_quit, scroll_offset = _run_command(
                worker, key.strip(), scroll_offset
            )
            if should_quit:
                return
            continue
        if key == "\x03":
            return
        if len(key) > 1 and ("\n" in key or "\r" in key):
            notice, should_quit, scroll_offset = _run_command(
                worker, key.strip(), scroll_offset
            )
            if should_quit:
                return
            continue
        if key in ("\r", "\n"):
            notice, should_quit, scroll_offset = _run_command(
                worker, command_buffer, scroll_offset
            )
            command_buffer = ""
            if should_quit:
                return
        elif key in ("\b", "\x7f"):
            command_buffer = command_buffer[:-1]
        elif key.isprintable():
            command_buffer += key


def _run_command(
    worker: MonitorWorker, raw: str, scroll_offset: int
) -> tuple[str, bool, int]:
    try:
        command = parse_monitor_command(raw)
    except ValueError as error:
        return f"Error: {error}", False, scroll_offset

    if command.kind == MonitorCommandKind.HELP:
        return (
            "Commands: help, pause, resume, clear, status, quit, scroll up, scroll down. "
            "Writes: hr 0=999, c 2=on, c 3=off. Input registers and discrete inputs are read-only.",
            False,
            scroll_offset,
        )
    if command.kind == MonitorCommandKind.PAUSE:
        try:
            worker.pause()
        except RuntimeError as error:
            return f"Error: {error}", False, scroll_offset
        return "Polling paused. Writes remain enabled.", False, scroll_offset
    if command.kind == MonitorCommandKind.RESUME:
        try:
            worker.resume()
        except RuntimeError as error:
            return f"Error: {error}", False, scroll_offset
        return "Polling resumed.", False, scroll_offset
    if command.kind == MonitorCommandKind.CLEAR:
        worker.state.clear_events()
        return "Change history cleared; current snapshots and configuration are unchanged.", False, scroll_offset
    if command.kind == MonitorCommandKind.STATUS:
        state = worker.state.display()
        ranges = ", ".join(
            f"{AREA_ABBREVIATIONS[area]}={item.offset}+{item.count}"
            for area, item in worker.config.ranges.items()
        )
        return (
            f"Target {worker.host}:{worker.port}, Unit ID {worker.unit_id}; "
            f"{state.status}, polling every {worker.config.interval:g}s; {ranges}.",
            False,
            scroll_offset,
        )
    if command.kind == MonitorCommandKind.SCROLL_UP:
        return "Showing earlier offsets.", False, max(0, scroll_offset - 5)
    if command.kind == MonitorCommandKind.SCROLL_DOWN:
        return "Showing later offsets.", False, scroll_offset + 5
    if command.kind == MonitorCommandKind.QUIT:
        return "Stopping monitor.", True, scroll_offset
    assert command.area is not None
    assert command.offset is not None
    assert command.value is not None
    try:
        worker.submit_write(command.area, command.offset, command.value)
    except RuntimeError as error:
        return f"Error: {error}", False, scroll_offset
    return (
        f"Queued write to {AREA_ABBREVIATIONS[command.area]}[{command.offset}].",
        False,
        scroll_offset,
    )
