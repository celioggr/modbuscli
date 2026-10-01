from collections.abc import Callable
from math import isfinite
import os
import socket
import sys
import time
from typing import Sequence

from consolemenu import MenuFormatBuilder, SelectionMenu
from consolemenu.format import MenuBorderStyleType
from consolemenu.items import FunctionItem
from consolemenu.screen import Screen
from pymodbus.client import ModbusTcpClient

from .operations import DataArea, read_values, validate_address_count, validate_write_values, write_values
from .sessions import OperationSession, SessionManager, run_once


def main() -> None:
    print("Modbus CLI - interactive Modbus TCP client")
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
            )
            if choice == -1:
                break
            if choice == 0:
                _client_menu(sessions, host, port, unit_id)
            elif choice == 1:
                host, port, unit_id = _configure_connection(host, port, unit_id)
    except (EOFError, KeyboardInterrupt):
        print("\nExiting.")
    finally:
        sessions.stop_all()


def _clear_screen() -> None:
    print("\033[2J\033[H", end="", flush=True)


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
) -> int:
    screen = RefreshingScreen(refresh_interval) if refresh_interval else None
    menu = LiveSelectionMenu(
        list(options),
        title=title,
        subtitle=subtitle,
        screen=screen,
        prologue_text=prologue_text,
        show_exit_option=show_exit_option,
        exit_option_text=exit_option_text,
        formatter=MenuFormatBuilder().set_border_style_type(
            MenuBorderStyleType.ASCII_BORDER
        ),
    )
    if screen is not None:
        marker = "__LIVE_STATUS_MARKER__"
        template = menu.formatter.format(
            title=menu.get_title(),
            subtitle=menu.get_subtitle(),
            prologue_text=marker,
            items=menu.items,
        )
        screen.configure_refresh(
            lambda: _render_prologue(menu),
            next(
                index
                for index, line in enumerate(template.splitlines())
                if marker in line
            )
            + 1,
        )
    menu.show()
    selected = menu.selected_option
    if show_exit_option and selected == len(options):
        return -1
    return selected


class RefreshingScreen(Screen):
    def __init__(self, refresh_interval: float) -> None:
        super().__init__()
        self.refresh_interval = refresh_interval
        self._refresh_callback: Callable[[], None] | None = None
        self._prologue_start_row = 0

    def configure_refresh(
        self, callback: Callable[[], None], prologue_start_row: int
    ) -> None:
        self._refresh_callback = callback
        self._prologue_start_row = prologue_start_row

    def refresh_prologue(self) -> None:
        if self._refresh_callback is not None:
            self._refresh_callback()

    def input(self, prompt: str = "") -> str:
        if os.name == "nt":
            import msvcrt

            deadline = time.monotonic() + self.refresh_interval
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
            readable, _, _ = select.select([sys.stdin], [], [], self.refresh_interval)
            return sys.stdin.read(1) if readable else ""
        finally:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, old_settings)


class LiveSelectionMenu(SelectionMenu):
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
                    self.screen.refresh_prologue()
                continue
            if user_input is None:
                self.should_exit = True
                continue
            for index, item in enumerate(self.items):
                if item.menu_char == user_input:
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


def _render_prologue(menu: LiveSelectionMenu) -> None:
    status = menu.get_prologue_text()
    if not isinstance(status, str):
        return
    rendered = menu.formatter.format(
        title=menu.get_title(),
        subtitle=menu.get_subtitle(),
        prologue_text=status,
        items=menu.items,
    ).splitlines()
    screen = menu.screen
    if not isinstance(screen, RefreshingScreen):
        return
    status_lines = status.splitlines()
    rows = rendered[screen._prologue_start_row - 1 :]
    rows = rows[: len(status_lines)]
    rows_up = len(rendered) - screen._prologue_start_row
    sys.stdout.write(f"\x1b[s\x1b[{rows_up}A\r")
    for index, row in enumerate(rows):
        sys.stdout.write(f"\x1b[2K{row}")
        if index < len(rows) - 1:
            sys.stdout.write("\x1b[1B\r")
    sys.stdout.write("\x1b[u")
    sys.stdout.flush()


def _configure_connection(host: str, port: int, unit_id: int) -> tuple[str, int, int]:
    _clear_screen()
    print("\nConfigure connection (press Enter to keep the current value)")
    new_host = input(f"Target hostname or IP address [{host}]: ").strip() or host
    new_port = _prompt_int("TCP port", 1, 65535, default=port)
    new_unit_id = _prompt_int("Unit ID", 0, 255, default=unit_id)
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
            exit_option_text="Back to main menu",
        )
        if choice == -1:
            return
        if choice == 0:
            last_message = _read_once(host, port, unit_id)
        elif choice == 1:
            last_message = _write_once(host, port, unit_id)
        elif choice == 2:
            last_message = _start_periodic_read(sessions, host, port, unit_id)
        elif choice == 3:
            last_message = _start_periodic_write(sessions, host, port, unit_id)
        elif choice == 4:
            last_message = _manage_sessions(sessions)


def _fit_status_line(text: str, width: int = 68) -> str:
    if len(text) <= width:
        return text
    return text[: width - 3] + "..."


def _read_once(host: str, port: int, unit_id: int) -> str:
    area = _select_area(writable_only=False)
    if area is None:
        return "Read cancelled."
    address, count = _prompt_read_range(area)

    while True:
        def operation(client: ModbusTcpClient) -> str:
            values = read_values(client, area, address, count, unit_id)
            return f"{area.value.title()} at address {address}: {values}"

        result = _run_and_report(host, port, unit_id, operation)
        choice = _show_menu(
            ("Read again with the same settings", "Read with new settings"),
            "Single read",
            subtitle=result,
            exit_option_text="Back to Client mode",
        )
        if choice == -1:
            return result
        if choice == 1:
            area = _select_area(writable_only=False)
            if area is None:
                return "Read cancelled."
            address, count = _prompt_read_range(area)


def _write_once(host: str, port: int, unit_id: int) -> str:
    area = _select_area(writable_only=True)
    if area is None:
        return "Write cancelled."
    address, values = _prompt_write_values(area)

    def perform_write() -> str:
        def operation(client: ModbusTcpClient) -> str:
            write_values(client, area, address, values, unit_id)
            return f"Wrote {values} to {area.value} at address {address}."

        return _run_and_report(host, port, unit_id, operation)

    result = perform_write()

    def write_with_new_settings() -> None:
        nonlocal area, address, values, result
        new_area = _select_area(writable_only=True)
        if new_area is None:
            result = "Write cancelled; previous settings retained."
            return
        area = new_area
        address, values = _prompt_write_values(area)
        result = perform_write()

    def update_result() -> None:
        nonlocal result
        result = perform_write()

    menu = LiveSelectionMenu(
        [],
        title="Single write",
        subtitle=lambda: result,
        prologue_text=lambda: f"Current settings: {area.menu_label}, address {address}, values {values}",
        formatter=MenuFormatBuilder().set_border_style_type(
            MenuBorderStyleType.ASCII_BORDER
        ),
    )
    menu.exit_item.text = "Back to Client mode"
    menu.append_item(
        FunctionItem(
            "Write again with the same settings",
            update_result,
            menu=menu,
            should_exit=False,
        )
    )
    menu.append_item(
        FunctionItem(
            "Write with new settings",
            write_with_new_settings,
            menu=menu,
            should_exit=False,
        )
    )
    menu.show()
    return result


def _start_periodic_read(sessions: SessionManager, host: str, port: int, unit_id: int) -> str:
    area = _select_area(writable_only=False)
    if area is None:
        return "Periodic read cancelled."
    address, count = _prompt_read_range(area)
    interval = _prompt_interval()
    session = sessions.start_read(host, port, unit_id, area, address, count, interval)
    _control_periodic_read(sessions, session)
    return (
        f"Periodic read session {session.session_id} ended after "
        f"{session.cycles} cycle(s). Latest values: {session.last_result}"
    )


def _control_periodic_read(sessions: SessionManager, session: OperationSession) -> None:
    while True:
        if session.state in ("running", "paused"):
            toggle_label = "Pause reading" if session.state == "running" else "Resume reading"
            options = (toggle_label, "Cancel read and return to Client mode")
        else:
            options = ("Return to Client mode",)
        choice = _show_menu(
            options,
            "Periodic read",
            subtitle=f"{session.description}; every {session.interval:g}s; {session.state}",
            prologue_text=lambda: _single_read_status(session),
            show_exit_option=False,
            refresh_interval=1 if session.state == "running" else None,
        )
        if session.state not in ("running", "paused"):
            sessions.stop(session.session_id)
            return
        if choice == 0:
            if session.state == "running":
                sessions.pause(session.session_id)
            else:
                sessions.resume(session.session_id)
        elif choice == 1 or choice == -1:
            sessions.stop(session.session_id)
            return


def _single_read_status(session: OperationSession) -> str:
    details = [
        f"Cycle {session.cycles}  |  Updated {session.updated_at:%H:%M:%S}",
        _fit_status_line(f"Latest values: {session.last_result}"),
        _fit_status_line(f"Latest error: {session.last_error or 'None'}"),
    ]
    return "\n".join(details)


def _start_periodic_write(
    sessions: SessionManager, host: str, port: int, unit_id: int
) -> str:
    area = _select_area(writable_only=True)
    if area is None:
        return "Periodic write cancelled."
    address, values = _prompt_write_values(area)
    interval = _prompt_interval()
    session = sessions.start_write(host, port, unit_id, area, address, values, interval)
    return (
        f"Started background write session {session.session_id}: "
        f"{session.description} every {interval:g}s."
    )


def _manage_sessions(sessions: SessionManager) -> str:
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
            exit_option_text="Return to client menu",
        )
        if choice == -1:
            return last_message
        if choice == 0:
            continue
        if choice == 1:
            session_id = _prompt_int("Background write session ID", 1, 2**31 - 1)
            if sessions.pause(session_id):
                last_message = f"Paused background write session {session_id}."
            else:
                last_message = f"Could not pause running session {session_id}."
        elif choice == 2:
            session_id = _prompt_int("Background write session ID", 1, 2**31 - 1)
            if sessions.resume(session_id):
                last_message = f"Resumed background write session {session_id}."
            else:
                last_message = f"Could not resume paused session {session_id}."
        elif choice == 3:
            session_id = _prompt_int("Session ID", 1, 2**31 - 1)
            if sessions.stop(session_id):
                last_message = f"Stopped session {session_id}."
            else:
                last_message = f"No session with ID {session_id}."
        elif choice == 4:
            sessions.stop_all()
            last_message = "All sessions stopped."


def _select_area(writable_only: bool) -> DataArea | None:
    areas = (
        (DataArea.COILS, DataArea.HOLDING_REGISTERS)
        if writable_only
        else tuple(DataArea)
    )
    selected = _show_menu(
        tuple(area.menu_label for area in areas),
        "Select data area",
        exit_option_text="Cancel",
    )
    if selected == -1:
        return None
    return areas[selected]


def _prompt_read_range(area: DataArea) -> tuple[int, int]:
    while True:
        address = _prompt_int("Starting address", 0, 65535)
        count = _prompt_int("Number of values", 1, 65536)
        try:
            validate_address_count(area, address, count)
            return address, count
        except ValueError as error:
            print(error)


def _prompt_write_values(area: DataArea) -> tuple[int, list[int]]:
    value_prompt = (
        "Coil value(s), comma-separated (for example 1,0,1): "
        if area == DataArea.COILS
        else "Register value(s), comma-separated (for example 123,456): "
    )
    while True:
        address = _prompt_int("Starting address", 0, 65535)
        raw_values = input(value_prompt).strip()
        try:
            values = [int(value.strip()) for value in raw_values.split(",")]
            validate_write_values(area, address, values)
            return address, values
        except ValueError as error:
            print(error)


def _prompt_interval() -> float:
    while True:
        raw_value = input("Interval in seconds (minimum 0.1): ").strip()
        try:
            interval = float(raw_value)
            if interval < 0.1 or not isfinite(interval):
                raise ValueError
            return interval
        except ValueError:
            print("Enter a finite interval of at least 0.1 seconds.")


def _prompt_int(
    label: str, minimum: int, maximum: int, default: int | None = None
) -> int:
    while True:
        try:
            prompt = f"{label} ({minimum}-{maximum})"
            if default is not None:
                prompt += f" [{default}]"
            raw_value = input(f"{prompt}: ").strip()
            if not raw_value and default is not None:
                return default
            value = int(raw_value)
            if minimum <= value <= maximum:
                return value
        except ValueError:
            pass
        print(f"Enter an integer between {minimum} and {maximum}.")


def _run_and_report(
    host: str, port: int, unit_id: int, operation: Callable[[ModbusTcpClient], str]
) -> str:
    try:
        return run_once(host, port, unit_id, operation)
    except Exception as error:
        return f"Operation failed: {error}"
