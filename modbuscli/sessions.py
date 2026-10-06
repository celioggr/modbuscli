from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from threading import Event, Lock, Thread, current_thread
from time import monotonic
from typing import Callable

from pymodbus.client import ModbusTcpClient

from .operations import DataArea, read_values, validate_address_count, validate_write_values, write_values

CLIENT_TIMEOUT_SECONDS = 3


@dataclass
class OperationSession:
    session_id: int
    description: str
    host: str
    port: int
    unit_id: int
    interval: float
    area: DataArea
    address: int
    count: int | None = None
    values: list[int] | None = None
    state: str = "running"
    cycles: int = 0
    last_result: str = "Waiting for first operation."
    last_values: list[int] | None = None
    last_error: str | None = None
    updated_at: datetime = field(default_factory=datetime.now)
    stop_event: Event = field(default_factory=Event, repr=False)
    pause_event: Event = field(default_factory=Event, repr=False)
    thread: Thread | None = field(default=None, repr=False)


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[int, OperationSession] = {}
        self._lock = Lock()

    def start_read(
        self, host: str, port: int, unit_id: int, area: DataArea, address: int, count: int, interval: float
    ) -> OperationSession:
        description = f"Read {count} {area.value} from address {address}"
        return self._start(host, port, unit_id, area, address, count, None, interval, description)

    def start_write(
        self, host: str, port: int, unit_id: int, area: DataArea, address: int, values: list[int], interval: float
    ) -> OperationSession:
        description = f"Write {len(values)} {area.value} at address {address}"
        return self._start(host, port, unit_id, area, address, None, values.copy(), interval, description)

    def _start(
        self,
        host: str,
        port: int,
        unit_id: int,
        area: DataArea,
        address: int,
        count: int | None,
        values: list[int] | None,
        interval: float,
        description: str,
    ) -> OperationSession:
        if not host.strip():
            raise ValueError("Target hostname or IP address cannot be empty.")
        if not 1 <= port <= 65535:
            raise ValueError("TCP port must be between 1 and 65535.")
        if not 0 <= unit_id <= 255:
            raise ValueError("Unit ID must be between 0 and 255.")
        if interval < 0.1 or not isfinite(interval):
            raise ValueError("Interval must be a finite value of at least 0.1 seconds.")
        if values is None:
            assert count is not None
            validate_address_count(area, address, count)
        else:
            validate_write_values(area, address, values)
        with self._lock:
            active_ids = {
                session_id
                for session_id, existing in self._sessions.items()
                if existing.state in ("running", "paused")
            }
            session_id = 1
            while session_id in active_ids:
                session_id += 1
            session = OperationSession(
                session_id, description, host, port, unit_id, interval, area, address, count, values
            )
            session.pause_event.set()
            session.thread = Thread(
                target=self._run,
                args=(session,),
                name=f"modbus-session-{session.session_id}",
                daemon=False,
            )
            self._sessions[session.session_id] = session
            session.thread.start()
        return session

    def list_sessions(self) -> list[OperationSession]:
        with self._lock:
            return list(self._sessions.values())

    def stop(self, session_id: int) -> bool:
        session = self._get(session_id)
        if session is None:
            return False
        session.stop_event.set()
        session.pause_event.set()
        self._join(session)
        return True

    def pause(self, session_id: int) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.state != "running":
                return False
            session.pause_event.clear()
            session.state = "paused"
            session.updated_at = datetime.now()
            return True

    def resume(self, session_id: int) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.state != "paused":
                return False
            session.pause_event.set()
            session.state = "running"
            session.updated_at = datetime.now()
            return True

    def stop_all(self) -> None:
        sessions = self.list_sessions()
        for session in sessions:
            session.stop_event.set()
            session.pause_event.set()
        for session in sessions:
            self._join(session)

    def _get(self, session_id: int) -> OperationSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    @staticmethod
    def _join(session: OperationSession) -> None:
        if session.thread is not None and session.thread is not current_thread():
            session.thread.join(CLIENT_TIMEOUT_SECONDS + 1)

    def _run(self, session: OperationSession) -> None:
        try:
            with ModbusTcpClient(session.host, port=session.port, timeout=CLIENT_TIMEOUT_SECONDS) as client:
                next_run = monotonic()
                while not session.stop_event.is_set():
                    session.pause_event.wait()
                    if session.stop_event.is_set():
                        break
                    try:
                        if not client.connected and not client.connect():
                            raise ConnectionError(
                                f"Could not connect to {session.host}:{session.port}."
                            )
                        read_result: list[int] | None = None
                        if session.values is None:
                            assert session.count is not None
                            read_result = read_values(
                                client, session.area, session.address, session.count, session.unit_id
                            )
                            result = f"Read {read_result}"
                        else:
                            write_values(
                                client, session.area, session.address, session.values, session.unit_id
                            )
                            result = f"Wrote {session.values}"
                        self._update(session, result=result, values=read_result)
                    except Exception as error:
                        client.close()
                        self._update(session, error=str(error))
                    next_run += session.interval
                    delay = max(0.0, next_run - monotonic())
                    next_run = max(next_run, monotonic())
                    if session.stop_event.wait(delay):
                        break
        except Exception as error:
            self._update(session, error=str(error), state="failed")
            return
        self._update(session, state="stopped")

    def _update(
        self,
        session: OperationSession,
        result: str | None = None,
        error: str | None = None,
        state: str | None = None,
        values: list[int] | None = None,
    ) -> None:
        with self._lock:
            session.cycles += 1 if result is not None or error is not None else 0
            if result is not None:
                session.last_result = result
                session.last_error = None
            if values is not None:
                session.last_values = values.copy()
            if error is not None:
                session.last_error = error
            if state is not None:
                session.state = state
            session.updated_at = datetime.now()


def run_once(
    host: str,
    port: int,
    unit_id: int,
    operation: Callable[[ModbusTcpClient], str],
) -> str:
    with ModbusTcpClient(host, port=port, timeout=CLIENT_TIMEOUT_SECONDS) as client:
        if not client.connect():
            raise ConnectionError(f"Could not connect to {host}:{port}.")
        return operation(client)
