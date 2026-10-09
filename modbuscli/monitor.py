from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from math import isfinite
import re
from queue import Empty, Queue
from threading import Event, Lock, Thread
from time import monotonic

from pymodbus.client import ModbusTcpClient

from .operations import (
    DataArea,
    read_values,
    validate_address_count,
    validate_write_values,
    write_values,
)
from .sessions import CLIENT_TIMEOUT_SECONDS

DEFAULT_POLL_INTERVAL = 1.0
# Keep only the most recent events to bound monitor memory use.
MAX_EVENT_HISTORY = 1000
DATA_AREAS = (
    DataArea.COILS,
    DataArea.DISCRETE_INPUTS,
    DataArea.INPUT_REGISTERS,
    DataArea.HOLDING_REGISTERS,
)
AREA_ABBREVIATIONS = {
    DataArea.COILS: "C",
    DataArea.DISCRETE_INPUTS: "DI",
    DataArea.INPUT_REGISTERS: "IR",
    DataArea.HOLDING_REGISTERS: "HR",
}
@dataclass(frozen=True)
class MonitorRange:
    offset: int = 0
    count: int = 10


def _default_ranges() -> dict[DataArea, MonitorRange]:
    return {area: MonitorRange() for area in DATA_AREAS}


@dataclass(frozen=True)
class MonitorConfig:
    ranges: dict[DataArea, MonitorRange] = field(default_factory=_default_ranges)
    interval: float = DEFAULT_POLL_INTERVAL

    def __post_init__(self) -> None:
        if set(self.ranges) != set(DATA_AREAS):
            raise ValueError("Monitor configuration must include all four Modbus data areas.")
        if not isfinite(self.interval) or self.interval < 0.1:
            raise ValueError("Polling interval must be a finite value of at least 0.1 seconds.")
        for area, address_range in self.ranges.items():
            validate_address_count(area, address_range.offset, address_range.count)


@dataclass(frozen=True)
class MonitorEvent:
    timestamp: datetime
    kind: str
    message: str

    def render(self) -> str:
        return f"{self.timestamp:%H:%M:%S}  {self.message}"


@dataclass(frozen=True)
class MonitorDisplayState:
    snapshots: dict[DataArea, tuple[int, ...]]
    changed: frozenset[tuple[DataArea, int]]
    errors: dict[DataArea, str]
    events: tuple[MonitorEvent, ...]
    paused: bool
    status: str


class MonitorState:
    def __init__(self, config: MonitorConfig) -> None:
        self.config = config
        self._lock = Lock()
        self._snapshots: dict[DataArea, tuple[int, ...]] = {}
        self._changed: set[tuple[DataArea, int]] = set()
        self._errors: dict[DataArea, str] = {}
        self._events: deque[MonitorEvent] = deque(maxlen=MAX_EVENT_HISTORY)
        self._pending_writes: dict[tuple[DataArea, int], int] = {}
        self._verification_errors: dict[tuple[DataArea, int], str] = {}
        self._paused = False
        self._status = "STARTING"

    def display(self) -> MonitorDisplayState:
        with self._lock:
            return MonitorDisplayState(
                snapshots=self._snapshots.copy(),
                changed=frozenset(self._changed),
                errors=self._errors.copy(),
                events=tuple(self._events),
                paused=self._paused,
                status=self._status,
            )

    def set_status(self, status: str) -> None:
        with self._lock:
            self._status = status

    def set_running(self) -> None:
        with self._lock:
            if not self._paused:
                self._status = "RUNNING"

    def set_fatal_error(self, error: str) -> None:
        with self._lock:
            self._append_event("ERROR", f"Monitor failed: {error}")
            self._status = "ERROR"

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self._paused = paused
            self._status = "PAUSED" if paused else "RUNNING"

    def clear_events(self) -> None:
        with self._lock:
            self._events.clear()

    def record_read(self, area: DataArea, values: list[int]) -> None:
        offset = self.config.ranges[area].offset
        with self._lock:
            previous = self._snapshots.get(area)
            self._changed = {
                entry for entry in self._changed if entry[0] != area
            }
            for index, value in enumerate(values):
                address = offset + index
                if previous is not None and index < len(previous) and previous[index] != value:
                    self._changed.add((area, address))
                    self._append_event(
                        "CHANGE",
                        f"{AREA_ABBREVIATIONS[area]}[{address}] "
                        f"{format_value(area, previous[index])} -> {format_value(area, value)}",
                    )
                pending_key = (area, address)
                if pending_key in self._pending_writes:
                    self._record_verification(pending_key, value)
            self._snapshots[area] = tuple(values)
            self._errors.pop(area, None)

    def record_read_error(self, area: DataArea, error: str) -> None:
        with self._lock:
            self._changed = {
                entry for entry in self._changed if entry[0] != area
            }
            if self._errors.get(area) != error:
                self._append_event(
                    "ERROR", f"{AREA_ABBREVIATIONS[area]} read failed: {error}"
                )
            self._errors[area] = error

    def record_write_attempt(self, area: DataArea, address: int, value: int) -> None:
        with self._lock:
            old_value = self._snapshot_value(area, address)
            if old_value is None:
                message = f"WRITE {AREA_ABBREVIATIONS[area]}[{address}] = {format_value(area, value)}"
            else:
                message = (
                    f"WRITE {AREA_ABBREVIATIONS[area]}[{address}] "
                    f"{format_value(area, old_value)} -> {format_value(area, value)}"
                )
            self._append_event("WRITE", message)

    def record_write_success(self, area: DataArea, address: int, value: int) -> None:
        with self._lock:
            self._pending_writes[(area, address)] = value

    def pending_verifications(self) -> tuple[tuple[DataArea, int, int], ...]:
        with self._lock:
            return tuple(
                (area, address, value)
                for (area, address), value in self._pending_writes.items()
                if not self._address_is_monitored(area, address)
            )

    def record_verification(self, area: DataArea, address: int, value: int) -> None:
        with self._lock:
            key = (area, address)
            if key in self._pending_writes:
                self._record_verification(key, value)
            self._verification_errors.pop(key, None)

    def record_verification_error(
        self, area: DataArea, address: int, error: str
    ) -> None:
        with self._lock:
            key = (area, address)
            if self._verification_errors.get(key) != error:
                self._append_event(
                    "VERIFY_FAILED",
                    f"VERIFY {AREA_ABBREVIATIONS[area]}[{address}] failed: {error}",
                )
            self._verification_errors[key] = error

    def record_write_error(
        self, area: DataArea, address: int, value: int, error: str
    ) -> None:
        with self._lock:
            self._append_event(
                "WRITE_FAILED",
                f"WRITE FAILED {AREA_ABBREVIATIONS[area]}[{address}] = "
                f"{format_value(area, value)}: {error}",
            )

    def _snapshot_value(self, area: DataArea, address: int) -> int | None:
        offset = self.config.ranges[area].offset
        values = self._snapshots.get(area)
        index = address - offset
        return values[index] if values is not None and 0 <= index < len(values) else None

    def _address_is_monitored(self, area: DataArea, address: int) -> bool:
        address_range = self.config.ranges[area]
        return address_range.offset <= address < address_range.offset + address_range.count

    def _record_verification(
        self, key: tuple[DataArea, int], observed: int
    ) -> None:
        area, address = key
        expected = self._pending_writes.pop(key)
        self._verification_errors.pop(key, None)
        if expected == observed:
            self._append_event(
                "VERIFIED",
                f"VERIFIED {AREA_ABBREVIATIONS[area]}[{address}] = "
                f"{format_value(area, observed)}",
            )
        else:
            self._append_event(
                "MISMATCH",
                f"MISMATCH {AREA_ABBREVIATIONS[area]}[{address}] expected "
                f"{format_value(area, expected)}, observed {format_value(area, observed)}",
            )

    def _append_event(self, kind: str, message: str) -> None:
        self._events.append(MonitorEvent(datetime.now(), kind, message))


def format_value(area: DataArea, value: int) -> str:
    if area in (DataArea.COILS, DataArea.DISCRETE_INPUTS):
        return "ON" if value else "OFF"
    return str(value)


class MonitorCommandKind(Enum):
    HELP = "help"
    PAUSE = "pause"
    RESUME = "resume"
    CLEAR = "clear"
    STATUS = "status"
    QUIT = "quit"
    SCROLL_UP = "scroll up"
    SCROLL_DOWN = "scroll down"
    WRITE = "write"


@dataclass(frozen=True)
class MonitorCommand:
    kind: MonitorCommandKind
    area: DataArea | None = None
    offset: int | None = None
    value: int | None = None


_WRITE_EXPRESSION = re.compile(r"([a-z-]+)\s+(\d+)\s*=\s*(\S+)", re.IGNORECASE)
_AREA_ALIASES = {
    "c": DataArea.COILS,
    "coil": DataArea.COILS,
    "coils": DataArea.COILS,
    "di": DataArea.DISCRETE_INPUTS,
    "discrete-input": DataArea.DISCRETE_INPUTS,
    "discrete-inputs": DataArea.DISCRETE_INPUTS,
    "ir": DataArea.INPUT_REGISTERS,
    "input-register": DataArea.INPUT_REGISTERS,
    "input-registers": DataArea.INPUT_REGISTERS,
    "hr": DataArea.HOLDING_REGISTERS,
    "holding": DataArea.HOLDING_REGISTERS,
    "holding-register": DataArea.HOLDING_REGISTERS,
    "holding-registers": DataArea.HOLDING_REGISTERS,
}
_SIMPLE_COMMANDS = {
    "help": MonitorCommandKind.HELP,
    "pause": MonitorCommandKind.PAUSE,
    "resume": MonitorCommandKind.RESUME,
    "clear": MonitorCommandKind.CLEAR,
    "status": MonitorCommandKind.STATUS,
    "quit": MonitorCommandKind.QUIT,
    "back": MonitorCommandKind.QUIT,
}


def parse_monitor_command(raw: str) -> MonitorCommand:
    command = raw.strip()
    lowered = command.casefold()
    if lowered in _SIMPLE_COMMANDS:
        return MonitorCommand(_SIMPLE_COMMANDS[lowered])
    if lowered in ("scroll up", "up"):
        return MonitorCommand(MonitorCommandKind.SCROLL_UP)
    if lowered in ("scroll down", "down"):
        return MonitorCommand(MonitorCommandKind.SCROLL_DOWN)
    match = _WRITE_EXPRESSION.fullmatch(command)
    if match is None:
        raise ValueError("Invalid command. Enter help to see supported commands.")
    alias, raw_offset, raw_value = match.groups()
    area = _AREA_ALIASES.get(alias.casefold())
    if area is None:
        raise ValueError(f"Unknown data area '{alias}'. Enter help to see supported commands.")
    if area == DataArea.INPUT_REGISTERS:
        raise ValueError("Input registers are read-only.")
    if area == DataArea.DISCRETE_INPUTS:
        raise ValueError("Discrete inputs are read-only.")
    offset = int(raw_offset)
    if area == DataArea.COILS:
        coil_values = {"on": 1, "1": 1, "off": 0, "0": 0}
        value = coil_values.get(raw_value.casefold())
        if value is None:
            raise ValueError("Coil value must be ON, OFF, 1, or 0.")
    else:
        try:
            value = int(raw_value, 10)
        except ValueError as error:
            raise ValueError("Register value must be an integer between 0 and 65535.") from error
    validate_write_values(area, offset, [value])
    return MonitorCommand(MonitorCommandKind.WRITE, area, offset, value)


@dataclass(frozen=True)
class _WriteRequest:
    area: DataArea
    offset: int
    value: int


class MonitorWorker:
    def __init__(
        self,
        host: str,
        port: int,
        unit_id: int,
        config: MonitorConfig,
    ) -> None:
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.config = config
        self.state = MonitorState(config)
        self._stop_event = Event()
        self._lifecycle_lock = Lock()
        self._commands: Queue[_WriteRequest | None] = Queue()
        self._thread = Thread(
            target=self._run,
            name="modbus-monitor",
            daemon=False,
        )

    def start(self) -> None:
        self._thread.start()

    def pause(self) -> None:
        with self._lifecycle_lock:
            self._ensure_running()
            self.state.set_paused(True)
            self._commands.put(None)

    def resume(self) -> None:
        with self._lifecycle_lock:
            self._ensure_running()
            self.state.set_paused(False)
            self._commands.put(None)

    def submit_write(self, area: DataArea, offset: int, value: int) -> None:
        with self._lifecycle_lock:
            self._ensure_running()
            self._commands.put(_WriteRequest(area, offset, value))

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop_event.set()
            self._commands.put(None)
        self._thread.join(CLIENT_TIMEOUT_SECONDS * 4 + 1)
        if self._thread.is_alive():
            raise RuntimeError("Monitor polling worker did not stop before the timeout.")

    def _ensure_running(self) -> None:
        status = self.state.display().status
        if self._stop_event.is_set() or not self._thread.is_alive():
            raise RuntimeError("Monitor worker is not running.")
        if status in ("ERROR", "STOPPED"):
            raise RuntimeError(f"Monitor worker is {status.lower()}.")

    def _run(self) -> None:
        client: ModbusTcpClient | None = None
        try:
            client = ModbusTcpClient(
                self.host, port=self.port, timeout=CLIENT_TIMEOUT_SECONDS
            )
            self.state.set_running()
            next_poll = monotonic()
            while True:
                if self._stop_event.is_set():
                    self._drain_writes(client)
                    break
                if self.state.display().paused:
                    timeout = None
                else:
                    timeout = max(0.0, next_poll - monotonic())
                try:
                    request = self._commands.get(timeout=timeout)
                except Empty:
                    self._poll(client)
                    next_poll = monotonic() + self.config.interval
                    continue
                if request is not None:
                    self._write(client, request)
        except Exception as error:
            self.state.set_fatal_error(str(error))
            self._fail_queued_writes(str(error))
        finally:
            if client is not None:
                client.close()
            if self._stop_event.is_set():
                self.state.set_status("STOPPED")

    def _drain_writes(self, client: ModbusTcpClient) -> None:
        while True:
            try:
                request = self._commands.get_nowait()
            except Empty:
                return
            if request is not None:
                self._write(client, request)

    def _fail_queued_writes(self, error: str) -> None:
        while True:
            try:
                request = self._commands.get_nowait()
            except Empty:
                return
            if request is not None:
                self.state.record_write_attempt(
                    request.area, request.offset, request.value
                )
                self.state.record_write_error(
                    request.area, request.offset, request.value, error
                )

    def _ensure_connected(self, client: ModbusTcpClient) -> None:
        if not client.connected and not client.connect():
            raise ConnectionError(f"Could not connect to {self.host}:{self.port}.")

    def _poll(self, client: ModbusTcpClient) -> None:
        try:
            self._ensure_connected(client)
        except Exception as error:
            for area in DATA_AREAS:
                self.state.record_read_error(area, str(error))
            client.close()
            return
        for area in DATA_AREAS:
            address_range = self.config.ranges[area]
            try:
                self._ensure_connected(client)
                values = read_values(
                    client,
                    area,
                    address_range.offset,
                    address_range.count,
                    self.unit_id,
                )
                self.state.record_read(area, values)
            except Exception as error:
                self.state.record_read_error(area, str(error))
                client.close()
        self._verify_out_of_range_writes(client)

    def _verify_out_of_range_writes(self, client: ModbusTcpClient) -> None:
        for area, address, _ in self.state.pending_verifications():
            try:
                self._ensure_connected(client)
                values = read_values(client, area, address, 1, self.unit_id)
                self.state.record_verification(area, address, values[0])
            except Exception as error:
                self.state.record_verification_error(area, address, str(error))
                client.close()

    def _write(self, client: ModbusTcpClient, request: _WriteRequest) -> None:
        self.state.record_write_attempt(request.area, request.offset, request.value)
        try:
            self._ensure_connected(client)
            write_values(
                client,
                request.area,
                request.offset,
                [request.value],
                self.unit_id,
            )
            self.state.record_write_success(
                request.area, request.offset, request.value
            )
        except Exception as error:
            self.state.record_write_error(
                request.area, request.offset, request.value, str(error)
            )
            client.close()
