from collections.abc import Callable
from math import isfinite
import os
import re
import shutil
import socket
import sys
import textwrap
import time
from enum import Enum
from typing import Sequence

from consolemenu import MenuFormatBuilder, SelectionMenu
from consolemenu.format import MenuBorderStyleType
from consolemenu.items import FunctionItem
from consolemenu.screen import Screen
from pymodbus.client import ModbusTcpClient

from .operations import DataArea, read_values, validate_address_count, validate_write_values, write_values
from .presentation import format_read_table
from .sessions import OperationSession, SessionManager, run_once


class NavigationAction(Enum):
    BACK = "back"
    BACK_TO_MAIN = "back_to_main"


BACK_LABEL = "Back (or press B)"
BACK_TO_MAIN_LABEL = "Back to Main Menu (or press M)"


def main() -> None:
    _print_banner()
    host = "127.0.0.1"
    port = 502
    unit_id = 1
    sessions = SessionManager()

    try:
        while True:
            choice = _show_menu(
                ("Client mode", "Configure connection"),
                "Modbus CLI",
                subtitle=_connection_subtitle(host, port, unit_id),
                navigation=False,
            )
            if choice == -1:
                break
            if choice == 0:
                _client_menu(sessions, host, port, unit_id)
            elif choice == 1:
                configured = _configure_connection(host, port, unit_id)
                if isinstance(configured, tuple):
                    host, port, unit_id = configured
    except (EOFError, KeyboardInterrupt):
        print("\nExiting.")
    finally:
        sessions.stop_all()


def _clear_screen() -> None:
    print("\033[2J\033[H", end="", flush=True)


def _supports_color() -> bool:
    return (
        sys.stdout.isatty()
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM", "").lower() != "dumb"
    )


def _paint(text: str, color: str, bold: bool = False) -> str:
    if not _supports_color():
        return text
    style = f"\033[{1 if bold else 0};{color}m"
    return f"{style}{text}\033[0m"


def _print_banner() -> None:
    banner = (
        "  +-------------------------------------------------------------------------+\n"
        "  |  MODBUS // MASTER                                                       |\n"
        "  |  Industrial protocol control console                                   |\n"
        "  +-------------------------------------------------------------------------+"
    )
    if _supports_color():
        lines = banner.splitlines()
        lines[0] = _paint(lines[0], "31", bold=True)
        lines[1] = _paint(lines[1], "91", bold=True)
        lines[2] = _paint(lines[2], "36")
        lines[3] = _paint(lines[3], "31", bold=True)
        banner = "\n".join(lines)
    print(banner)


def _print_dialog(title: str, message: str) -> None:
    content_width = 72
    wrapped_lines = [
        line
        for paragraph in message.splitlines()
        for line in (textwrap.wrap(paragraph, width=content_width - 4) or [""])
    ]
    width = max(content_width, len(title) + 4, *(len(line) + 4 for line in wrapped_lines))
    border = f"  +{'-' * width}+"
    print(_paint(border, "31", bold=True))
    print(
        f"  |  {_paint(title, '91', bold=True)}"
        f"{' ' * max(0, width - len(title) - 2)}|"
    )
    for line in wrapped_lines:
        print(f"  |  {line:<{width - 2}}|")
    print(_paint(border, "31", bold=True))


def _boxed_input(title: str, prompt: str, allow_navigation: bool = True) -> str:
    navigation_text = f"{BACK_LABEL}  |  {BACK_TO_MAIN_LABEL}"
    content_width = max(
        72,
        len(title) + 4,
        len(prompt) + 4,
        len(navigation_text) + 4 if allow_navigation else 0,
    )
    border = f"  +{'-' * content_width}+"
    print(_paint(border, "31", bold=True))
    print(
        f"  |  {_paint(title, '91', bold=True)}"
        f"{' ' * max(0, content_width - len(title) - 2)}|"
    )
    if allow_navigation:
        print(f"  |  {navigation_text:<{content_width - 2}}|")
    prefix = f"  |  {prompt}: "
    print(_paint(prefix, "96", bold=True), end="", flush=True)
    value = input().strip()
    print(f"  |{' ' * (content_width - 1)}|")
    print(_paint(border, "31", bold=True))
    return value


def _style_menu_text(text: str, title: str | None = None) -> str:
    if not _supports_color():
        return text

    styled_lines = []
    for line in text.splitlines():
        if line.lstrip().startswith("+"):
            styled_lines.append(_paint(line, "31", bold=True))
        elif title and title in line:
            styled_lines.append(_paint(line, "91", bold=True))
        elif re.fullmatch(r"\s*\|\s*\d+\s+-\s+.*\|\s*", line):
            match = re.search(r"\d+ - ", line)
            assert match is not None
            styled_lines.append(
                f"{line[:match.start()]}{_paint(match.group(), '93', bold=True)}"
                f"{_paint(line[match.end():], '96', bold=True)}"
            )
        elif "Target:" in line:
            line = line.replace("Target:", _paint("Target:", "96", bold=True))
            line = line.replace("Unit ID:", _paint("Unit ID:", "93", bold=True))
            line = line.replace("Connected", _paint("Connected", "92", bold=True))
            line = re.sub(r"Error:", lambda match: _paint(match.group(), "91", bold=True), line)
            styled_lines.append(line)
        elif "Latest error:" in line:
            styled_lines.append(_paint(line, "91"))
        elif "[running]" in line:
            styled_lines.append(line.replace("[running]", _paint("[running]", "92", bold=True)))
        elif "[paused]" in line:
            styled_lines.append(line.replace("[paused]", _paint("[paused]", "93", bold=True)))
        elif line.lstrip().startswith(">>"):
            styled_lines.append(_paint(line, "91", bold=True))
        else:
            styled_lines.append(line)
    return "\n".join(styled_lines)


def _connection_subtitle(host: str, port: int, unit_id: int) -> str:
    try:
        with socket.create_connection((host, port), timeout=1):
            status = "Connected"
    except OSError as error:
        status = f"Error: {error}"
    return f"Target: {host}:{port}  |  Unit ID: {unit_id} | {status}"


def _show_menu(
    options: Sequence[str],
    title: str,
    subtitle: str | None = None,
    prologue_text: str | Callable[[], str] | None = None,
    exit_option_text: str = "Exit",
    show_exit_option: bool = True,
    refresh_interval: float | None = None,
    navigation: bool = True,
) -> int:
    screen = (
        RefreshingScreen(refresh_interval)
        if refresh_interval
        else KeypressScreen()
    )
    menu = LiveSelectionMenu(
        list(options),
        title=title,
        subtitle=subtitle,
        screen=screen,
        prologue_text=prologue_text,
        show_exit_option=show_exit_option and not navigation,
        exit_option_text=exit_option_text,
        formatter=MenuFormatBuilder().set_border_style_type(
            MenuBorderStyleType.ASCII_BORDER
        ),
    )
    if navigation:
        menu.append_item(
            FunctionItem(
                BACK_LABEL,
                lambda: None,
                menu=menu,
                should_exit=True,
            )
        )
        menu.append_item(
            FunctionItem(
                BACK_TO_MAIN_LABEL,
                lambda: None,
                menu=menu,
                should_exit=True,
            )
        )
    menu.show()
    selected = menu.selected_option
    if navigation and selected == len(options):
        return -1
    if navigation and selected == len(options) + 1:
        return -2
    if show_exit_option and selected == len(options):
        return -1
    return selected


class KeypressScreen(Screen):
    def input(self, prompt: str = "") -> str:
        return self._read_key(prompt)

    def _read_key(self, prompt: str = "", timeout: float | None = None) -> str:
        if os.name == "nt":
            import msvcrt

            if timeout is None:
                return msvcrt.getwch()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if msvcrt.kbhit():
                    return msvcrt.getwch()
                time.sleep(0.05)
            return ""
        if not sys.stdin.isatty():
            return super().input(prompt)

        import select
        import termios
        import tty

        descriptor = sys.stdin.fileno()
        old_settings = termios.tcgetattr(descriptor)
        try:
            tty.setcbreak(descriptor)
            if timeout is not None:
                readable, _, _ = select.select([sys.stdin], [], [], timeout)
                if not readable:
                    return ""
            return sys.stdin.read(1)
        finally:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, old_settings)


class RefreshingScreen(KeypressScreen):
    def __init__(self, refresh_interval: float) -> None:
        super().__init__()
        self.refresh_interval = refresh_interval
        self.rendered_lines: list[str] | None = None

    def input(self, prompt: str = "") -> str:
        return self._read_key(prompt, timeout=self.refresh_interval)


class LiveSelectionMenu(SelectionMenu):
    def draw(self):
        rendered = self.formatter.format(
            title=self.get_title(),
            subtitle=self.get_subtitle(),
            items=self.items,
            prologue_text=self.get_prologue_text(),
            epilogue_text=self.get_epilogue_text(),
        )
        if isinstance(self.screen, RefreshingScreen):
            self.screen.rendered_lines = rendered.splitlines()
        self.screen.printf(_style_menu_text(rendered, self.get_title()))

    def refresh_dynamic_content(self) -> None:
        if not isinstance(self.screen, RefreshingScreen):
            return
        rendered = self.formatter.format(
            title=self.get_title(),
            subtitle=self.get_subtitle(),
            items=self.items,
            prologue_text=self.get_prologue_text(),
            epilogue_text=self.get_epilogue_text(),
        )
        new_lines = rendered.splitlines()
        old_lines = self.screen.rendered_lines
        if old_lines is None or len(old_lines) != len(new_lines):
            self.clear_screen()
            self.draw()
            return

        visible_rows = shutil.get_terminal_size(fallback=(80, 24)).lines
        last_index = len(new_lines) - 1
        updates = [
            (last_index - index, line)
            for index, (old, line) in enumerate(zip(old_lines, new_lines))
            if old != line and last_index - index < visible_rows
        ]
        if updates:
            sys.stdout.write("\033[s")
            for rows_up, line in updates:
                cursor_up = f"\033[{rows_up}A" if rows_up else ""
                sys.stdout.write(
                    f"\033[u{cursor_up}\r\033[2K"
                    f"{_style_menu_text(line, self.get_title())}"
                )
            sys.stdout.write("\033[u")
            sys.stdout.flush()
        self.screen.rendered_lines = new_lines

    def _main_loop(self) -> None:
        self._set_up_colors()
        SelectionMenu.currently_active_menu = self
        self._running.set()
        self.clear_screen()
        self.draw()

        while self._running.is_set() and not self.should_exit:
            user_input = self.get_input()
            if user_input == "":
                if isinstance(self.screen, RefreshingScreen):
                    self.refresh_dynamic_content()
                continue
            if user_input is None:
                self.should_exit = True
                continue
            shortcut_label = {
                "b": BACK_LABEL,
                "m": BACK_TO_MAIN_LABEL,
            }.get(user_input.casefold())
            for index, item in enumerate(self.items):
                if (
                    item.menu_char
                    and item.menu_char.casefold() == user_input.casefold()
                ) or (shortcut_label is not None and item.get_text() == shortcut_label):
                    self.current_option = index
                    self.select()
                    break
            else:
                try:
                    selected = int(user_input)
                except ValueError:
                    continue
                if 0 < selected <= len(self.items):
                    self.current_option = selected - 1
                    self.select()
                    if not self.should_exit:
                        self.draw()


def _configure_connection(
    host: str, port: int, unit_id: int
) -> tuple[str, int, int] | NavigationAction:
    _clear_screen()
    new_host = _boxed_input(
        "Configure connection (press Enter to keep current values)",
        f"Target hostname or IP address [{host}]",
        allow_navigation=True,
    ) or host
    navigation = _parse_navigation_action(new_host)
    if navigation is not None:
        return navigation
    new_port = _prompt_int("TCP port", 1, 65535, default=port)
    if isinstance(new_port, NavigationAction):
        return new_port
    new_unit_id = _prompt_int("Unit ID", 0, 255, default=unit_id)
    if isinstance(new_unit_id, NavigationAction):
        return new_unit_id
    return new_host, new_port, new_unit_id


def _client_menu(sessions: SessionManager, host: str, port: int, unit_id: int) -> None:
    last_message = ""
    while True:
        subtitle = _connection_subtitle(host, port, unit_id)
        if last_message:
            subtitle += f"\n{last_message}"
        choice = _show_menu(
            (
                "Read once",
                "Write once",
                "Start periodic read",
                "Start periodic write",
                "Manage sessions",
            ),
            "Client mode",
            subtitle=subtitle,
        )
        if choice in (-1, -2):
            return
        if choice == 0:
            result = _read_once(host, port, unit_id)
        elif choice == 1:
            result = _write_once(host, port, unit_id)
        elif choice == 2:
            result = _start_periodic_read(sessions, host, port, unit_id)
        elif choice == 3:
            result = _start_periodic_write(sessions, host, port, unit_id)
        elif choice == 4:
            result = _manage_sessions(sessions)
        else:
            continue
        if result is NavigationAction.BACK_TO_MAIN:
            return
        if result is NavigationAction.BACK:
            last_message = ""
            continue
        if choice in (0, 2):
            last_message = ""
            continue
        last_message = result


def _read_once(host: str, port: int, unit_id: int) -> str | NavigationAction:
    settings = _select_read_settings()
    if isinstance(settings, NavigationAction):
        return settings
    area, address, count = settings

    run_operation = True
    while True:
        if run_operation:
            def operation(client: ModbusTcpClient) -> str:
                values = read_values(client, area, address, count, unit_id)
                return format_read_table(area, address, values)

            result = _run_and_report(host, port, unit_id, operation)
            run_operation = False
        choice = _show_menu(
            (
                "Read again with the same settings",
                "Read with new settings",
            ),
            "Single read",
            subtitle=result,
        )
        if choice == -1:
            return result
        if choice == -2:
            return NavigationAction.BACK_TO_MAIN
        if choice == 0:
            run_operation = True
        if choice == 1:
            settings = _select_read_settings()
            if settings is NavigationAction.BACK_TO_MAIN:
                return NavigationAction.BACK_TO_MAIN
            if settings is NavigationAction.BACK:
                continue
            area, address, count = settings
            run_operation = True


def _write_once(host: str, port: int, unit_id: int) -> str | NavigationAction:
    settings = _select_write_settings()
    if isinstance(settings, NavigationAction):
        return settings
    area, address, values = settings

    def perform_write() -> str:
        def operation(client: ModbusTcpClient) -> str:
            write_values(client, area, address, values, unit_id)
            return f"Wrote {values} to {area.value} at address {address}."

        return _run_and_report(host, port, unit_id, operation)

    result = perform_write()

    navigation = NavigationAction.BACK
    new_settings_item: FunctionItem

    def write_with_new_settings() -> None:
        nonlocal area, address, values, result, navigation
        settings = _select_write_settings()
        if settings is NavigationAction.BACK_TO_MAIN:
            navigation = NavigationAction.BACK_TO_MAIN
            new_settings_item.should_exit = True
            return
        if settings is NavigationAction.BACK:
            return
        area, address, values = settings
        result = perform_write()

    def update_result() -> None:
        nonlocal result
        result = perform_write()

    menu = LiveSelectionMenu(
        [],
        title="Single write",
        subtitle=lambda: result,
        prologue_text=lambda: f"Current settings: {area.menu_label}, address {address}, values {values}",
        show_exit_option=False,
        formatter=MenuFormatBuilder().set_border_style_type(
            MenuBorderStyleType.ASCII_BORDER
        ),
    )
    menu.append_item(
        FunctionItem(
            "Write again with the same settings",
            update_result,
            menu=menu,
            should_exit=False,
        )
    )
    new_settings_item = FunctionItem(
        "Write with new settings",
        write_with_new_settings,
        menu=menu,
        should_exit=False,
    )
    menu.append_item(new_settings_item)
    menu.append_item(
        FunctionItem(
            BACK_LABEL,
            lambda: None,
            menu=menu,
            should_exit=True,
        )
    )
    menu.append_item(
        FunctionItem(
            BACK_TO_MAIN_LABEL,
            lambda: None,
            menu=menu,
            should_exit=True,
        )
    )
    menu.show()
    if navigation is NavigationAction.BACK_TO_MAIN:
        return NavigationAction.BACK_TO_MAIN
    if menu.selected_option == len(menu.items) - 1:
        return NavigationAction.BACK_TO_MAIN
    if menu.selected_option == len(menu.items) - 2:
        return result
    return result


def _start_periodic_read(
    sessions: SessionManager, host: str, port: int, unit_id: int
) -> str | NavigationAction:
    while True:
        settings = _select_read_settings()
        if isinstance(settings, NavigationAction):
            return settings
        area, address, count = settings
        interval = _prompt_interval()
        if interval is NavigationAction.BACK:
            continue
        if interval is NavigationAction.BACK_TO_MAIN:
            return interval
        break
    assert interval is not None
    session = sessions.start_read(host, port, unit_id, area, address, count, interval)
    navigation = _control_periodic_read(sessions, session)
    if navigation is NavigationAction.BACK_TO_MAIN:
        return NavigationAction.BACK_TO_MAIN
    return (
        f"Periodic read session {session.session_id} ended after "
        f"{session.cycles} cycle(s)."
    )


def _control_periodic_read(
    sessions: SessionManager, session: OperationSession
) -> NavigationAction:
    assert session.count is not None
    page_size = max(1, shutil.get_terminal_size(fallback=(80, 24)).lines - 23)
    page_count = max(1, (session.count + page_size - 1) // page_size)
    page_index = 0

    while True:
        if session.state in ("running", "paused"):
            toggle_label = "Pause reading" if session.state == "running" else "Resume reading"
            options = [toggle_label]
            if page_count > 1:
                if page_index < page_count - 1:
                    options.append("Next table page")
                if page_index > 0:
                    options.append("Previous table page")
        else:
            options = []
        choice = _show_menu(
            tuple(options),
            "Periodic read",
            subtitle=f"{session.description}; every {session.interval:g}s; {session.state}",
            prologue_text=lambda: _single_read_status(
                session, page_index * page_size, page_size, page_count
            ),
            refresh_interval=1 if session.state == "running" else None,
        )
        if session.state not in ("running", "paused"):
            sessions.stop(session.session_id)
            return NavigationAction.BACK_TO_MAIN if choice == -2 else NavigationAction.BACK
        if choice == 0:
            if session.state == "running":
                sessions.pause(session.session_id)
            else:
                sessions.resume(session.session_id)
        elif choice == -2:
            sessions.stop(session.session_id)
            return NavigationAction.BACK_TO_MAIN
        elif choice == -1:
            sessions.stop(session.session_id)
            return NavigationAction.BACK
        elif 0 <= choice < len(options):
            selected_action = options[choice]
            if selected_action == "Next table page":
                page_index += 1
            elif selected_action == "Previous table page":
                page_index -= 1


def _single_read_status(
    session: OperationSession,
    offset_start: int = 0,
    page_size: int | None = None,
    page_count: int = 1,
) -> str:
    count = session.count
    row_count = (
        min(page_size, max(0, count - offset_start))
        if page_size is not None and count is not None
        else count
    )
    values = (
        session.last_values[offset_start : offset_start + row_count]
        if session.last_values is not None and row_count is not None
        else session.last_values
    )
    details = [f"Cycle {session.cycles}  |  Updated {session.updated_at:%H:%M:%S}"]
    if page_count > 1 and row_count is not None:
        details.append(
            f"Showing offsets {offset_start}-{offset_start + row_count - 1} "
            f"(page {offset_start // (page_size or 1) + 1}/{page_count})"
        )
    details.extend((
        format_read_table(
            session.area,
            session.address,
            values,
            row_count,
            offset_start=offset_start,
        ),
        f"Latest error: {session.last_error or 'None'}",
    ))
    return "\n".join(details)


def _start_periodic_write(
    sessions: SessionManager, host: str, port: int, unit_id: int
) -> str | NavigationAction:
    while True:
        settings = _select_write_settings()
        if isinstance(settings, NavigationAction):
            return settings
        area, address, values = settings
        interval = _prompt_interval(optional=True)
        if interval is NavigationAction.BACK:
            continue
        if interval is NavigationAction.BACK_TO_MAIN:
            return interval
        break
    session = sessions.start_write(host, port, unit_id, area, address, values, interval)
    cadence = "as fast as possible" if interval is None else f"every {interval:g}s"
    return (
        f"Started background write session {session.session_id}: "
        f"{session.description} {cadence}."
    )


def _manage_sessions(sessions: SessionManager) -> str | NavigationAction:
    last_message = ""
    while True:
        active_sessions = [
            session for session in sessions.list_sessions() if session.state in ("running", "paused")
        ]
        details = ["No active sessions."] if not active_sessions else []
        for session in active_sessions:
            details.append(
                f"#{session.session_id} [{session.state}] {session.description} "
                f"at {session.host}:{session.port}, unit {session.unit_id}; "
                f"{session.cycles} cycles; updated {session.updated_at:%H:%M:%S}"
            )
            if session.values is None:
                details.append(
                    format_read_table(
                        session.area,
                        session.address,
                        session.last_values,
                        session.count,
                    )
                )
            else:
                details.append(f"  Latest result: {session.last_result}")
            if session.last_error:
                details.append(f"  Latest error: {session.last_error}")
        if last_message:
            details.append(last_message)
        choice = _show_menu(
            (
                "Refresh",
                "Pause a background write",
                "Resume a background write",
                "Stop a session",
                "Stop all sessions",
            ),
            "Background sessions",
            subtitle=f"{len(active_sessions)} active session(s)",
            prologue_text="\n".join(details),
        )
        if choice == -1:
            return last_message
        if choice == -2:
            return NavigationAction.BACK_TO_MAIN
        if choice == 0:
            continue
        if choice == 1:
            session_id = _prompt_int("Background write session ID", 1, 2**31 - 1)
            if isinstance(session_id, NavigationAction):
                if session_id is NavigationAction.BACK_TO_MAIN:
                    return session_id
                continue
            if sessions.pause(session_id):
                last_message = f"Paused background write session {session_id}."
            else:
                last_message = f"Could not pause running session {session_id}."
        elif choice == 2:
            session_id = _prompt_int("Background write session ID", 1, 2**31 - 1)
            if isinstance(session_id, NavigationAction):
                if session_id is NavigationAction.BACK_TO_MAIN:
                    return session_id
                continue
            if sessions.resume(session_id):
                last_message = f"Resumed background write session {session_id}."
            else:
                last_message = f"Could not resume paused session {session_id}."
        elif choice == 3:
            session_id = _prompt_int("Session ID", 1, 2**31 - 1)
            if isinstance(session_id, NavigationAction):
                if session_id is NavigationAction.BACK_TO_MAIN:
                    return session_id
                continue
            if sessions.stop(session_id):
                last_message = f"Stopped session {session_id}."
            else:
                last_message = f"No session with ID {session_id}."
        elif choice == 4:
            sessions.stop_all()
            last_message = "All sessions stopped."


def _select_read_settings(
) -> tuple[DataArea, int, int] | NavigationAction:
    while True:
        area = _select_area(writable_only=False)
        if area is NavigationAction.BACK_TO_MAIN:
            return NavigationAction.BACK_TO_MAIN
        if area is None:
            return NavigationAction.BACK
        read_range = _prompt_read_range(area)
        if read_range is NavigationAction.BACK:
            continue
        if read_range is NavigationAction.BACK_TO_MAIN:
            return NavigationAction.BACK_TO_MAIN
        address, count = read_range
        return area, address, count


def _select_write_settings(
) -> tuple[DataArea, int, list[int]] | NavigationAction:
    while True:
        area = _select_area(writable_only=True)
        if area is NavigationAction.BACK_TO_MAIN:
            return NavigationAction.BACK_TO_MAIN
        if area is None:
            return NavigationAction.BACK
        write_input = _prompt_write_values(area)
        if write_input is NavigationAction.BACK:
            continue
        if write_input is NavigationAction.BACK_TO_MAIN:
            return NavigationAction.BACK_TO_MAIN
        address, values = write_input
        return area, address, values


def _select_area(writable_only: bool) -> DataArea | NavigationAction | None:
    areas = (
        (DataArea.COILS, DataArea.HOLDING_REGISTERS)
        if writable_only
        else tuple(DataArea)
    )
    selected = _show_menu(
        tuple(area.menu_label for area in areas),
        "Select data area",
    )
    if selected == -1:
        return None
    if selected == -2:
        return NavigationAction.BACK_TO_MAIN
    return areas[selected]


def _prompt_read_range(area: DataArea) -> tuple[int, int] | NavigationAction:
    while True:
        address = _prompt_int("Starting address", 0, 65535, allow_navigation=True)
        if isinstance(address, NavigationAction):
            return address
        count = _prompt_int(
            "Number of values to read", 1, 65536
        )
        if isinstance(count, NavigationAction):
            if count is NavigationAction.BACK:
                continue
            return count
        try:
            validate_address_count(area, address, count)
            return address, count
        except ValueError as error:
            _print_dialog("Invalid read range", str(error))


def _prompt_write_values(
    area: DataArea,
) -> tuple[int, list[int]] | NavigationAction:
    value_prompt = (
        "Coil value(s), comma-separated (for example 1,0,1)"
        if area == DataArea.COILS
        else "Register value(s), comma-separated (for example 123,456)"
    )
    title = f"Write {area.menu_label}"
    while True:
        address = _prompt_int("Starting address", 0, 65535)
        if isinstance(address, NavigationAction):
            return address
        raw_values = _boxed_input(title, value_prompt, allow_navigation=True)
        navigation = _parse_navigation_action(raw_values)
        if navigation is not None:
            if navigation is NavigationAction.BACK:
                continue
            return navigation
        try:
            values = [int(value.strip()) for value in raw_values.split(",")]
            validate_write_values(area, address, values)
            return address, values
        except ValueError as error:
            _print_dialog("Invalid write values", str(error))


def _prompt_interval(
    optional: bool = False,
) -> float | None | NavigationAction:
    while True:
        prompt = (
            "Interval in seconds (minimum 0.1; leave blank for as fast as possible)"
            if optional
            else "Interval in seconds (minimum 0.1)"
        )
        raw_value = _boxed_input(
            "Periodic interval",
            prompt,
            allow_navigation=True,
        )
        navigation = _parse_navigation_action(raw_value)
        if navigation is not None:
            return navigation
        if optional and not raw_value.strip():
            return None
        try:
            interval = float(raw_value)
            if interval < 0.1 or not isfinite(interval):
                raise ValueError
            return interval
        except ValueError:
            _print_dialog(
                "Invalid interval", "Enter a finite interval of at least 0.1 seconds."
            )


def _prompt_int(
    label: str,
    minimum: int,
    maximum: int,
    default: int | None = None,
    allow_navigation: bool = True,
) -> int | NavigationAction:
    while True:
        try:
            prompt = f"{label} ({minimum}-{maximum})"
            if default is not None:
                prompt += f" [{default}]"
            raw_value = _boxed_input(
                "Enter a value", prompt, allow_navigation=allow_navigation
            )
            if allow_navigation:
                navigation = _parse_navigation_action(raw_value)
                if navigation is not None:
                    return navigation
            if not raw_value and default is not None:
                return default
            value = int(raw_value)
            if minimum <= value <= maximum:
                return value
        except ValueError:
            pass
        _print_dialog(
            "Invalid value", f"Enter an integer between {minimum} and {maximum}."
        )


def _parse_navigation_action(value: str) -> NavigationAction | None:
    normalized = value.casefold()
    if normalized in ("b", "back"):
        return NavigationAction.BACK
    if normalized in ("m", "main", "back to main menu"):
        return NavigationAction.BACK_TO_MAIN
    return None


def _run_and_report(
    host: str, port: int, unit_id: int, operation: Callable[[ModbusTcpClient], str]
) -> str:
    try:
        return run_once(host, port, unit_id, operation)
    except Exception as error:
        return f"Operation failed: {error}"
