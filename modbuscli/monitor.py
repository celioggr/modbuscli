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
class ContinuousWriteDisplay:
    writer_id: int
    area: DataArea
    offset: int
    values: tuple[int, ...]
    interval: float | None
    paused: bool


@dataclass(frozen=True)
class MonitorDisplayState:
    snapshots: dict[DataArea, tuple[int, ...]]
    changed: frozenset[tuple[DataArea, int]]
    errors: dict[DataArea, str]
    events: tuple[MonitorEvent, ...]
    continuous_writes: tuple[ContinuousWriteDisplay, ...]
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
        self._continuous_writes: tuple[ContinuousWriteDisplay, ...] = ()
        self._pending_writes: dict[tuple[DataArea, int], int] = {}
        self._verification_errors: dict[tuple[DataArea, int], str] = {}
        self._continuous_errors: dict[int, str] = {}
        self._paused = False
        self._status = "STARTING"

    def display(self) -> MonitorDisplayState:
        with self._lock:
            return MonitorDisplayState(
                snapshots=self._snapshots.copy(),
                changed=frozenset(self._changed),
                errors=self._errors.copy(),
                events=tuple(self._events),
                continuous_writes=self._continuous_writes,
                paused=self._paused,
                status=self._status,
            )

    def set_continuous_writes(
        self, writers: tuple[ContinuousWriteDisplay, ...]
    ) -> None:
        with self._lock:
            self._continuous_writes = writers

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

    def record_continuous_write(
        self,
        kind: str,
        writer_id: int,
        area: DataArea,
        offset: int,
        values: tuple[int, ...],
        interval: float | None,
    ) -> None:
        end_offset = offset + len(values) - 1
        address = (
            f"{AREA_ABBREVIATIONS[area]}[{offset}]"
            if end_offset == offset
            else f"{AREA_ABBREVIATIONS[area]}[{offset}-{end_offset}]"
        )
        cadence = "as fast as possible" if interval is None else f"every {interval:g}s"
        value = format_value(area, values[0])
        actions = {
            "CONTINUOUS_STARTED": "CONTINUOUS WRITE STARTED",
            "CONTINUOUS_PAUSED": "CONTINUOUS WRITE PAUSED",
            "CONTINUOUS_RESUMED": "CONTINUOUS WRITE RESUMED",
            "CONTINUOUS_STOPPED": "CONTINUOUS WRITE STOPPED",
        }
        with self._lock:
            self._append_event(
                kind, f"{actions[kind]} #{writer_id} {address} = {value} ({cadence})"
            )

    def record_continuous_write_error(
        self,
        writer_id: int,
        area: DataArea,
        offset: int,
        values: tuple[int, ...],
        error: str,
    ) -> None:
        end_offset = offset + len(values) - 1
        key = writer_id
        address = f"{AREA_ABBREVIATIONS[area]}[{offset}-{end_offset}]"
        with self._lock:
            if self._continuous_errors.get(key) != error:
                self._append_event(
                    "WRITE_FAILED",
                    f"CONTINUOUS WRITE FAILED #{writer_id} {address}: {error}",
                )
            self._continuous_errors[key] = error

    def clear_continuous_write_error(
        self, writer_id: int
    ) -> None:
        with self._lock:
            self._continuous_errors.pop(writer_id, None)

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
    START_CONTINUOUS = "start continuous write"
    PAUSE_CONTINUOUS = "pause continuous write"
    RESUME_CONTINUOUS = "resume continuous write"
    STOP_CONTINUOUS = "stop continuous write"


@dataclass(frozen=True)
class MonitorCommand:
    kind: MonitorCommandKind
    area: DataArea | None = None
    offset: int | None = None
    value: int | None = None
    end_offset: int | None = None
    interval: float | None = None
    writer_id: int | None = None


_WRITE_EXPRESSION = re.compile(
    r"([a-z-]+)\s+(\d+)(?:-(\d+))?\s*=\s*(\S+)", re.IGNORECASE
)
_CONTINUOUS_EXPRESSION = re.compile(
    r"repeat\s+([a-z-]+)\s+(\d+)(?:-(\d+))?\s*=\s*(\S+)"
    r"(?:\s+every\s+(\d+(?:\.\d+)?)s)?",
    re.IGNORECASE,
)
_CONTINUOUS_CONTROL_EXPRESSION = re.compile(
    r"(pause|resume|stop)\s+(\d+)",
    re.IGNORECASE,
)
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
    continuous = _CONTINUOUS_EXPRESSION.fullmatch(command)
    continuous_control = _CONTINUOUS_CONTROL_EXPRESSION.fullmatch(command)
    if continuous_control is not None:
        action, raw_writer_id = continuous_control.groups()
        writer_id = int(raw_writer_id)
        if writer_id <= 0:
            raise ValueError("Continuous write ID must be greater than zero.")
        command_kind = {
            "pause": MonitorCommandKind.PAUSE_CONTINUOUS,
            "resume": MonitorCommandKind.RESUME_CONTINUOUS,
            "stop": MonitorCommandKind.STOP_CONTINUOUS,
        }[action.casefold()]
        return MonitorCommand(
            command_kind,
            writer_id=writer_id,
        )

    if continuous is not None:
        alias, raw_offset, raw_end, raw_value, raw_interval = continuous.groups()
        area = _writable_area(alias)
        offset = int(raw_offset)
        end_offset = int(raw_end) if raw_end is not None else offset
        value = _parse_write_value(area, raw_value)
        _validate_write_range(area, offset, end_offset, value)
        interval = float(raw_interval) if raw_interval is not None else None
        if interval is not None and (not isfinite(interval) or interval <= 0):
            raise ValueError("Continuous write interval must be a finite number greater than zero.")
        return MonitorCommand(
            MonitorCommandKind.START_CONTINUOUS,
            area,
            offset,
            value,
            end_offset,
            interval,
        )

    match = _WRITE_EXPRESSION.fullmatch(command)
    if match is None:
        raise ValueError("Invalid command. Enter help to see supported commands.")
    alias, raw_offset, raw_end, raw_value = match.groups()
    area = _writable_area(alias)
    offset = int(raw_offset)
    end_offset = int(raw_end) if raw_end is not None else offset
    value = _parse_write_value(area, raw_value)
    _validate_write_range(area, offset, end_offset, value)
    return MonitorCommand(
        MonitorCommandKind.WRITE,
        area,
        offset,
        value,
        end_offset,
    )


def _writable_area(alias: str) -> DataArea:
    area = _AREA_ALIASES.get(alias.casefold())
    if area is None:
        raise ValueError(f"Unknown data area '{alias}'. Enter help to see supported commands.")
    if area == DataArea.INPUT_REGISTERS:
        raise ValueError("Input registers are read-only.")
    if area == DataArea.DISCRETE_INPUTS:
        raise ValueError("Discrete inputs are read-only.")
    return area


def _parse_write_value(area: DataArea, raw_value: str) -> int:
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
    return value


def _validate_write_range(
    area: DataArea, offset: int, end_offset: int, value: int
) -> None:
    if end_offset < offset:
        raise ValueError("Write range end offset must be greater than or equal to its start.")
    count = end_offset - offset + 1
    max_count = 1968 if area == DataArea.COILS else 123
    if count > max_count:
        raise ValueError(f"Write count cannot exceed {max_count} for {area.value}.")
    validate_write_values(area, offset, [value] * count)


@dataclass(frozen=True)
class _WriteRequest:
    area: DataArea
    offset: int
    values: tuple[int, ...]


@dataclass(frozen=True)
class _StartContinuousRequest:
    writer_id: int
    area: DataArea
    offset: int
    values: tuple[int, ...]
    interval: float | None


@dataclass(frozen=True)
class _StopContinuousRequest:
    writer_id: int


@dataclass(frozen=True)
class _SetContinuousPausedRequest:
    writer_id: int
    paused: bool


@dataclass
class _ContinuousWriter:
    writer_id: int
    area: DataArea
    offset: int
    values: tuple[int, ...]
    interval: float | None
    next_run: float
    paused: bool = False
    last_error: str | None = None

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
        self._commands: Queue[
            _WriteRequest
            | _StartContinuousRequest
            | _StopContinuousRequest
            | _SetContinuousPausedRequest
            | None
        ] = Queue()
        self._continuous_writers: dict[int, _ContinuousWriter] = {}
        self._next_continuous_id = 1
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

    def submit_write(
        self, area: DataArea, offset: int, values: int | list[int]
    ) -> None:
        write_values_list = [values] if isinstance(values, int) else values
        validate_write_values(area, offset, write_values_list)
        with self._lifecycle_lock:
            self._ensure_running()
            self._commands.put(
                _WriteRequest(area, offset, tuple(write_values_list))
            )

    def start_continuous_write(
        self,
        area: DataArea,
        offset: int,
        values: list[int],
        interval: float | None = None,
    ) -> int:
        validate_write_values(area, offset, values)
        if interval is not None and (not isfinite(interval) or interval <= 0):
            raise ValueError(
                "Continuous write interval must be a finite number greater than zero."
            )
        with self._lifecycle_lock:
            self._ensure_running()
            writer_id = self._next_continuous_id
            self._next_continuous_id += 1
            self._commands.put(
                _StartContinuousRequest(
                    writer_id, area, offset, tuple(values), interval
                )
            )
            return writer_id

    def stop_continuous_write(self, writer_id: int) -> None:
        self._control_continuous_write(_StopContinuousRequest(writer_id))

    def pause_continuous_write(self, writer_id: int) -> None:
        self._control_continuous_write(
            _SetContinuousPausedRequest(writer_id, True)
        )

    def resume_continuous_write(self, writer_id: int) -> None:
        self._control_continuous_write(
            _SetContinuousPausedRequest(writer_id, False)
        )

    def _control_continuous_write(
        self,
        request: _StopContinuousRequest | _SetContinuousPausedRequest,
    ) -> None:
        if request.writer_id <= 0:
            raise ValueError("Continuous write ID must be greater than zero.")
        with self._lifecycle_lock:
            self._ensure_running()
            self._commands.put(request)

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
                now = monotonic()
                deadlines = []
                if not self.state.display().paused:
                    deadlines.append(next_poll)
                deadlines.extend(
                    writer.next_run
                    for writer in self._continuous_writers.values()
                    if writer.interval is not None and not writer.paused
                )
                if any(
                    writer.interval is None and not writer.paused
                    for writer in self._continuous_writers.values()
                ):
                    timeout = 0
                elif deadlines:
                    timeout = max(0.0, min(deadlines) - now)
                else:
                    timeout = None
                try:
                    request = self._commands.get(timeout=timeout)
                except Empty:
                    request = None
                if isinstance(request, _WriteRequest):
                    self._write(client, request)
                elif isinstance(request, _StartContinuousRequest):
                    self._start_continuous(request)
                elif isinstance(request, _StopContinuousRequest):
                    self._stop_continuous(request)
                elif isinstance(request, _SetContinuousPausedRequest):
                    self._set_continuous_writer_paused(request)
                if self._stop_event.is_set():
                    continue
                now = monotonic()
                if (
                    not self.state.display().paused
                    and now >= next_poll
                ):
                    self._poll(client)
                    next_poll = monotonic() + self.config.interval
                self._run_continuous_writers(client)
        except Exception as error:
            self.state.set_fatal_error(str(error))
            self._fail_queued_writes(str(error))
        finally:
            if client is not None:
                client.close()
            for writer in tuple(self._continuous_writers.values()):
                self._stop_writer(writer)
            self._continuous_writers.clear()
            self._publish_continuous_writers()
            if self._stop_event.is_set():
                self.state.set_status("STOPPED")

    def _drain_writes(self, client: ModbusTcpClient) -> None:
        while True:
            try:
                request = self._commands.get_nowait()
            except Empty:
                break
            if request is not None:
                if isinstance(request, _WriteRequest):
                    self._write(client, request)
                elif isinstance(request, _StartContinuousRequest):
                    self._start_continuous(request)
                elif isinstance(request, _StopContinuousRequest):
                    self._stop_continuous(request)
                elif isinstance(request, _SetContinuousPausedRequest):
                    self._set_continuous_writer_paused(request)
        for writer in tuple(self._continuous_writers.values()):
            self._stop_writer(writer)
        self._continuous_writers.clear()
        self._publish_continuous_writers()

    def _start_continuous(self, request: _StartContinuousRequest) -> None:
        writer = _ContinuousWriter(
            request.writer_id,
            request.area,
            request.offset,
            request.values,
            request.interval,
            monotonic(),
        )
        self._continuous_writers[writer.writer_id] = writer
        self._publish_continuous_writers()
        self.state.record_continuous_write(
            "CONTINUOUS_STARTED", writer.writer_id, writer.area, writer.offset, writer.values,
            writer.interval,
        )

    def _stop_continuous(self, request: _StopContinuousRequest) -> None:
        writer = self._continuous_writers.pop(request.writer_id, None)
        if writer is not None:
            self._stop_writer(writer)
            self._publish_continuous_writers()

    def _set_continuous_writer_paused(
        self, request: _SetContinuousPausedRequest
    ) -> None:
        writer = self._continuous_writers.get(request.writer_id)
        if writer is not None and writer.paused != request.paused:
            writer.paused = request.paused
            kind = "CONTINUOUS_PAUSED" if request.paused else "CONTINUOUS_RESUMED"
            self.state.record_continuous_write(
                kind, writer.writer_id, writer.area, writer.offset,
                writer.values, writer.interval
            )
            self._publish_continuous_writers()

    def _publish_continuous_writers(self) -> None:
        self.state.set_continuous_writes(
            tuple(
                ContinuousWriteDisplay(
                    writer.writer_id,
                    writer.area,
                    writer.offset,
                    writer.values,
                    writer.interval,
                    writer.paused,
                )
                for writer in self._continuous_writers.values()
            )
        )

    def _stop_writer(self, writer: _ContinuousWriter) -> None:
        self.state.record_continuous_write(
            "CONTINUOUS_STOPPED", writer.writer_id, writer.area,
            writer.offset, writer.values,
            writer.interval,
        )

    def _run_continuous_writers(self, client: ModbusTcpClient) -> None:
        now = monotonic()
        for writer in tuple(self._continuous_writers.values()):
            if writer.paused:
                continue
            if writer.interval is not None and now < writer.next_run:
                continue
            try:
                self._ensure_connected(client)
                write_values(
                    client,
                    writer.area,
                    writer.offset,
                    list(writer.values),
                    self.unit_id,
                )
                if writer.last_error is not None:
                    self.state.clear_continuous_write_error(writer.writer_id)
                    writer.last_error = None
            except Exception as error:
                error_message = str(error)
                if writer.last_error != error_message:
                    self.state.record_continuous_write_error(
                        writer.writer_id, writer.area, writer.offset,
                        writer.values, error_message
                    )
                    writer.last_error = error_message
                client.close()
            if writer.interval is not None:
                writer.next_run = monotonic() + writer.interval

    def _fail_queued_writes(self, error: str) -> None:
        while True:
            try:
                request = self._commands.get_nowait()
            except Empty:
                return
            if request is not None:
                if isinstance(request, _WriteRequest):
                    for index, value in enumerate(request.values):
                        self.state.record_write_attempt(
                            request.area, request.offset + index, value
                        )
                        self.state.record_write_error(
                            request.area, request.offset + index, value, error
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
        for index, value in enumerate(request.values):
            self.state.record_write_attempt(request.area, request.offset + index, value)
        try:
            self._ensure_connected(client)
            write_values(
                client,
                request.area,
                request.offset,
                list(request.values),
                self.unit_id,
            )
            for index, value in enumerate(request.values):
                self.state.record_write_success(
                    request.area, request.offset + index, value
                )
        except Exception as error:
            for index, value in enumerate(request.values):
                self.state.record_write_error(
                    request.area, request.offset + index, value, str(error)
                )
            client.close()
