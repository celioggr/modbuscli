import time
import unittest
import re
from datetime import datetime, timedelta
from unittest.mock import patch

from modbuscli import app
from modbuscli.monitor import (
    AREA_ABBREVIATIONS,
    ContinuousWriteDisplay,
    DATA_AREAS,
    MAX_EVENT_HISTORY,
    MonitorCommandKind,
    MonitorConfig,
    MonitorEvent,
    MonitorRange,
    MonitorState,
    MonitorWorker,
    parse_monitor_command,
)
from modbuscli.monitor_view import render_monitor, update_monitor_screen
from modbuscli.operations import DataArea


class Response:
    def __init__(self, count):
        self.bits = [True] * count
        self.registers = [123] * count

    def isError(self):
        return False


class FakeClient:
    connected = True

    def __init__(self, *args, **kwargs):
        self.calls = []
        self.read_addresses = []
        self.writes = []
        self.closed = False
        self.fail_discrete_inputs = False

    def connect(self):
        self.connected = True
        return True

    def close(self):
        self.closed = True
        self.connected = False

    def _read(self, name, address, count):
        self.calls.append(name)
        self.read_addresses.append((name, address))
        if name == "read_discrete_inputs" and self.fail_discrete_inputs:
            raise TimeoutError("simulated timeout")
        return Response(count)

    def read_coils(self, address, count, **kwargs):
        return self._read("read_coils", address, count)

    def read_discrete_inputs(self, address, count, **kwargs):
        return self._read("read_discrete_inputs", address, count)

    def read_input_registers(self, address, count, **kwargs):
        return self._read("read_input_registers", address, count)

    def read_holding_registers(self, address, count, **kwargs):
        return self._read("read_holding_registers", address, count)

    def write_register(self, address, value, **kwargs):
        self.calls.append("write_register")
        self.writes.append(("write_register", address, (value,)))
        return Response(1)

    def write_registers(self, address, values, **kwargs):
        self.calls.append("write_registers")
        self.writes.append(("write_registers", address, tuple(values)))
        return Response(len(values))

    def write_coil(self, address, value, **kwargs):
        self.calls.append("write_coil")
        self.writes.append(("write_coil", address, (int(value),)))
        return Response(1)

    def write_coils(self, address, values, **kwargs):
        self.calls.append("write_coils")
        self.writes.append(("write_coils", address, tuple(map(int, values))))
        return Response(1)


class MonitorTests(unittest.TestCase):
    def test_default_configuration_monitors_ten_entries_from_zero(self):
        config = MonitorConfig()

        self.assertEqual(config.interval, 1.0)
        self.assertEqual(
            config.ranges,
            {area: MonitorRange(0, 10) for area in DATA_AREAS},
        )

    def test_each_area_accepts_its_own_offset_and_count(self):
        ranges = {
            DataArea.COILS: MonitorRange(10, 20),
            DataArea.DISCRETE_INPUTS: MonitorRange(21, 5),
            DataArea.INPUT_REGISTERS: MonitorRange(100, 3),
            DataArea.HOLDING_REGISTERS: MonitorRange(500, 8),
        }

        config = MonitorConfig(ranges, interval=0.5)

        self.assertEqual(config.ranges, ranges)
        self.assertEqual(config.interval, 0.5)

    def test_monitor_config_rejects_invalid_ranges_and_interval(self):
        ranges = {area: MonitorRange() for area in DATA_AREAS}
        ranges[DataArea.HOLDING_REGISTERS] = MonitorRange(65535, 2)
        with self.assertRaises(ValueError):
            MonitorConfig(ranges)
        with self.assertRaises(ValueError):
            MonitorConfig(interval=0)

    def test_snapshot_comparison_detects_changes_in_every_area(self):
        state = MonitorState(MonitorConfig())
        initial = {
            DataArea.COILS: [0],
            DataArea.DISCRETE_INPUTS: [0],
            DataArea.INPUT_REGISTERS: [10],
            DataArea.HOLDING_REGISTERS: [20],
        }
        for area, values in initial.items():
            state.record_read(area, values)
        for area, values in initial.items():
            state.record_read(area, [1 if area in (DataArea.COILS, DataArea.DISCRETE_INPUTS) else values[0] + 1])

        events = state.display().events
        self.assertEqual(
            {event.message.split("[", 1)[0] for event in events},
            {AREA_ABBREVIATIONS[area] for area in DATA_AREAS},
        )
        self.assertEqual(len(events), 4)

    def test_unchanged_values_do_not_create_change_events(self):
        state = MonitorState(MonitorConfig())
        state.record_read(DataArea.HOLDING_REGISTERS, [123])
        state.record_read(DataArea.HOLDING_REGISTERS, [123])

        self.assertEqual(state.display().events, ())

    def test_write_commands_parse_with_zero_based_offsets(self):
        holding = parse_monitor_command("hr 0=999")
        coil_on = parse_monitor_command("c 2=on")
        coil_off = parse_monitor_command("c 3=off")

        self.assertEqual(
            (holding.kind, holding.area, holding.offset, holding.value),
            (MonitorCommandKind.WRITE, DataArea.HOLDING_REGISTERS, 0, 999),
        )
        self.assertEqual((coil_on.area, coil_on.offset, coil_on.value), (DataArea.COILS, 2, 1))
        self.assertEqual((coil_off.area, coil_off.offset, coil_off.value), (DataArea.COILS, 3, 0))

    def test_coil_values_are_case_insensitive_and_accept_zero_one(self):
        for expression, expected in (
            ("C 0=ON", 1),
            ("coil 0=OFF", 0),
            ("c 0=1", 1),
            ("c 0=0", 0),
        ):
            with self.subTest(expression=expression):
                self.assertEqual(parse_monitor_command(expression).value, expected)

    def test_range_write_parses_inclusive_offsets_and_replicated_value(self):
        command = parse_monitor_command("c 1-10=on")
        register_command = parse_monitor_command("hr 40-42=999")

        self.assertEqual(
            (command.offset, command.end_offset, command.value),
            (1, 10, 1),
        )
        self.assertEqual(
            (
                register_command.area,
                register_command.offset,
                register_command.end_offset,
                register_command.value,
            ),
            (DataArea.HOLDING_REGISTERS, 40, 42, 999),
        )

    def test_continuous_write_commands_parse_default_and_custom_interval(self):
        fastest = parse_monitor_command("repeat c 1-10=on")
        timed = parse_monitor_command("repeat hr 4=321 every 0.25s")
        pause = parse_monitor_command("pause 1")
        resume = parse_monitor_command("resume 1")
        stop = parse_monitor_command("stop 1")

        self.assertEqual(fastest.kind, MonitorCommandKind.START_CONTINUOUS)
        self.assertIsNone(fastest.interval)
        self.assertEqual(timed.interval, 0.25)
        self.assertEqual(timed.value, 321)
        self.assertEqual(pause.kind, MonitorCommandKind.PAUSE_CONTINUOUS)
        self.assertEqual(pause.writer_id, 1)
        self.assertEqual(resume.kind, MonitorCommandKind.RESUME_CONTINUOUS)
        self.assertEqual(stop.kind, MonitorCommandKind.STOP_CONTINUOUS)
        self.assertEqual(stop.writer_id, 1)
        with self.assertRaisesRegex(ValueError, "ID must be greater than zero"):
            parse_monitor_command("pause 0")

    def test_invalid_write_ranges_and_continuous_intervals_are_rejected(self):
        for expression in (
            "c 10-1=on",
            "hr 0-123=1",
            "c 65535-65536=on",
            "repeat c 0=on every 0s",
            "repeat hr 0=1 every 1e999s",
        ):
            with self.subTest(expression=expression):
                with self.assertRaises(ValueError):
                    parse_monitor_command(expression)

    def test_read_only_data_areas_are_rejected(self):
        for expression, message in (
            ("ir 0=100", "Input registers are read-only."),
            ("di 2=on", "Discrete inputs are read-only."),
        ):
            with self.subTest(expression=expression):
                with self.assertRaisesRegex(ValueError, message):
                    parse_monitor_command(expression)

    def test_invalid_commands_and_register_values_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid command"):
            parse_monitor_command("write hr 0 99")
        for expression in ("hr 0=-1", "hr 0=65536", "hr 0=1.5", "c 0=maybe"):
            with self.subTest(expression=expression):
                with self.assertRaises(ValueError):
                    parse_monitor_command(expression)

    def test_pause_and_resume_update_state(self):
        worker = MonitorWorker("localhost", 502, 1, MonitorConfig())
        client = FakeClient()
        with patch("modbuscli.monitor.ModbusTcpClient", return_value=client):
            worker.start()
            worker.pause()
            self.assertEqual(worker.state.display().status, "PAUSED")
            self.assertTrue(worker.state.display().paused)

            worker.resume()
            self.assertEqual(worker.state.display().status, "RUNNING")
            self.assertFalse(worker.state.display().paused)
            worker.stop()

    def test_clear_removes_events_without_changing_snapshot_or_config(self):
        config = MonitorConfig()
        state = MonitorState(config)
        state.record_read(DataArea.HOLDING_REGISTERS, [123])
        state.record_read(DataArea.HOLDING_REGISTERS, [456])

        state.clear_events()

        self.assertEqual(state.display().events, ())
        self.assertEqual(state.display().snapshots[DataArea.HOLDING_REGISTERS], (456,))
        self.assertIs(state.config, config)

    def test_write_verification_records_verified_and_mismatch_events(self):
        verified = MonitorState(MonitorConfig())
        verified.record_read(DataArea.HOLDING_REGISTERS, [123])
        verified.record_write_attempt(DataArea.HOLDING_REGISTERS, 0, 999)
        verified.record_write_success(DataArea.HOLDING_REGISTERS, 0, 999)
        verified.record_read(DataArea.HOLDING_REGISTERS, [999])
        self.assertTrue(any(event.kind == "VERIFIED" for event in verified.display().events))

        mismatch = MonitorState(MonitorConfig())
        mismatch.record_read(DataArea.HOLDING_REGISTERS, [123])
        mismatch.record_write_attempt(DataArea.HOLDING_REGISTERS, 0, 999)
        mismatch.record_write_success(DataArea.HOLDING_REGISTERS, 0, 999)
        mismatch.record_read(DataArea.HOLDING_REGISTERS, [123])
        self.assertTrue(any(event.kind == "MISMATCH" for event in mismatch.display().events))

    def test_out_of_range_write_is_logged_without_inventing_old_value(self):
        state = MonitorState(MonitorConfig())
        state.record_write_attempt(DataArea.HOLDING_REGISTERS, 500, 999)
        state.record_write_success(DataArea.HOLDING_REGISTERS, 500, 999)

        self.assertEqual(state.display().events[0].message, "WRITE HR[500] = 999")
        self.assertEqual(
            state.pending_verifications(),
            ((DataArea.HOLDING_REGISTERS, 500, 999),),
        )

    def test_event_history_is_bounded(self):
        state = MonitorState(MonitorConfig())
        for index in range(MAX_EVENT_HISTORY + 5):
            state.record_write_error(DataArea.HOLDING_REGISTERS, 0, index % 65536, "failed")

        self.assertEqual(len(state.display().events), MAX_EVENT_HISTORY)

    def test_repeated_identical_read_errors_are_deduplicated(self):
        state = MonitorState(MonitorConfig())
        state.record_read_error(DataArea.HOLDING_REGISTERS, "connection timeout")
        state.record_read_error(DataArea.HOLDING_REGISTERS, "connection timeout")
        state.record_read_error(DataArea.HOLDING_REGISTERS, "server unavailable")

        self.assertEqual(
            [event.message for event in state.display().events],
            [
                "HR read failed: connection timeout",
                "HR read failed: server unavailable",
            ],
        )

    def test_worker_executes_and_verifies_an_interactive_write(self):
        client = FakeClient()
        worker = MonitorWorker(
            "localhost", 502, 1, MonitorConfig(interval=0.1)
        )
        with patch("modbuscli.monitor.ModbusTcpClient", return_value=client):
            worker.start()
            deadline = time.monotonic() + 2
            while len(worker.state.display().snapshots) < 4 and time.monotonic() < deadline:
                time.sleep(0.01)
            worker.submit_write(DataArea.HOLDING_REGISTERS, 0, 999)
            deadline = time.monotonic() + 2
            while (
                not any(event.kind == "MISMATCH" for event in worker.state.display().events)
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            events = worker.state.display().events
            worker.stop()

        self.assertIn("write_register", client.calls)
        self.assertTrue(any(event.kind == "WRITE" for event in events))
        self.assertTrue(any(event.kind == "MISMATCH" for event in events))

    def test_worker_executes_multiple_writes_as_a_single_range_write(self):
        client = FakeClient()
        worker = MonitorWorker("localhost", 502, 1, MonitorConfig(interval=1))
        with patch("modbuscli.monitor.ModbusTcpClient", return_value=client):
            worker.start()
            worker.submit_write(DataArea.COILS, 1, [1] * 10)
            deadline = time.monotonic() + 2
            while not client.writes and time.monotonic() < deadline:
                time.sleep(0.01)
            worker.stop()

        self.assertIn(("write_coils", 1, (1,) * 10), client.writes)

    def test_continuous_write_runs_until_stopped(self):
        client = FakeClient()
        worker = MonitorWorker("localhost", 502, 1, MonitorConfig(interval=1))
        with patch("modbuscli.monitor.ModbusTcpClient", return_value=client):
            worker.start()
            writer_id = worker.start_continuous_write(
                DataArea.COILS, 1, [1] * 10
            )
            deadline = time.monotonic() + 2
            while len(client.writes) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            worker.stop_continuous_write(writer_id)
            deadline = time.monotonic() + 2
            while (
                not any(
                    event.kind == "CONTINUOUS_STOPPED"
                    for event in worker.state.display().events
                )
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            stopped_count = len(client.writes)
            time.sleep(0.05)
            final_count = len(client.writes)
            events = worker.state.display().events
            worker.stop()

        self.assertGreaterEqual(stopped_count, 2)
        self.assertEqual(final_count, stopped_count)
        self.assertTrue(any(event.kind == "CONTINUOUS_STARTED" for event in events))
        self.assertTrue(any(event.kind == "CONTINUOUS_STOPPED" for event in events))

    def test_timed_continuous_write_repeats_after_requested_interval(self):
        client = FakeClient()
        worker = MonitorWorker("localhost", 502, 1, MonitorConfig(interval=1))
        with patch("modbuscli.monitor.ModbusTcpClient", return_value=client):
            worker.start()
            worker.start_continuous_write(
                DataArea.HOLDING_REGISTERS, 4, [321], interval=0.05
            )
            deadline = time.monotonic() + 2
            while len(client.writes) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            worker.stop()

        self.assertGreaterEqual(len(client.writes), 2)
        self.assertTrue(all(call[0] == "write_register" for call in client.writes))

    def test_continuous_write_can_be_paused_and_resumed(self):
        client = FakeClient()
        worker = MonitorWorker("localhost", 502, 1, MonitorConfig(interval=1))
        with patch("modbuscli.monitor.ModbusTcpClient", return_value=client):
            worker.start()
            writer_id = worker.start_continuous_write(
                DataArea.COILS, 1, [1] * 10
            )
            deadline = time.monotonic() + 2
            while len(client.writes) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            worker.pause_continuous_write(writer_id)
            deadline = time.monotonic() + 2
            while (
                not worker.state.display().continuous_writes
                or not worker.state.display().continuous_writes[0].paused
            ) and time.monotonic() < deadline:
                time.sleep(0.01)
            paused_count = len(client.writes)
            time.sleep(0.05)
            count_while_paused = len(client.writes)
            worker.resume_continuous_write(writer_id)
            deadline = time.monotonic() + 2
            while len(client.writes) == count_while_paused and time.monotonic() < deadline:
                time.sleep(0.01)
            worker.stop()

        self.assertEqual(count_while_paused, paused_count)
        self.assertGreater(len(client.writes), count_while_paused)

    def test_worker_reads_back_write_outside_monitored_range(self):
        ranges = MonitorConfig().ranges.copy()
        ranges[DataArea.HOLDING_REGISTERS] = MonitorRange(0, 1)
        config = MonitorConfig(ranges=ranges, interval=0.1)
        client = FakeClient()
        worker = MonitorWorker("localhost", 502, 1, config)
        with patch("modbuscli.monitor.ModbusTcpClient", return_value=client):
            worker.start()
            deadline = time.monotonic() + 2
            while len(worker.state.display().snapshots) < 4 and time.monotonic() < deadline:
                time.sleep(0.01)
            worker.submit_write(DataArea.HOLDING_REGISTERS, 500, 999)
            deadline = time.monotonic() + 2
            while (
                not any(event.kind == "MISMATCH" for event in worker.state.display().events)
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            events = worker.state.display().events
            worker.stop()

        self.assertIn(("read_holding_registers", 500), client.read_addresses)
        self.assertTrue(any(event.kind == "MISMATCH" for event in events))

    def test_worker_stops_and_releases_its_connection(self):
        client = FakeClient()
        worker = MonitorWorker("localhost", 502, 1, MonitorConfig())
        with patch("modbuscli.monitor.ModbusTcpClient", return_value=client):
            worker.start()
            deadline = time.monotonic() + 2
            while len(worker.state.display().snapshots) < 4 and time.monotonic() < deadline:
                time.sleep(0.01)
            worker.stop()

        self.assertFalse(worker._thread.is_alive())
        self.assertTrue(client.closed)
        self.assertEqual(worker.state.display().status, "STOPPED")

    def test_failed_area_read_does_not_stop_other_area_reads(self):
        client = FakeClient()
        client.fail_discrete_inputs = True
        worker = MonitorWorker("localhost", 502, 1, MonitorConfig())
        with patch("modbuscli.monitor.ModbusTcpClient", return_value=client):
            worker.start()
            deadline = time.monotonic() + 2
            while (
                DataArea.HOLDING_REGISTERS not in worker.state.display().snapshots
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            state = worker.state.display()
            worker.stop()

        self.assertIn(DataArea.DISCRETE_INPUTS, state.errors)
        self.assertIn(DataArea.COILS, state.snapshots)
        self.assertIn(DataArea.INPUT_REGISTERS, state.snapshots)
        self.assertIn(DataArea.HOLDING_REGISTERS, state.snapshots)

    def test_render_fits_four_column_and_two_by_two_layouts(self):
        state = MonitorState(MonitorConfig())
        display = state.display()

        wide = render_monitor(display, state.config.ranges, "localhost", 502, 1, 1, "", width=120, height=30)
        narrow = render_monitor(display, state.config.ranges, "localhost", 502, 1, 1, "", width=80, height=24)
        compact = render_monitor(display, state.config.ranges, "localhost", 502, 1, 1, "", width=80, height=12)

        self.assertIn("Coils", wide)
        self.assertIn("Holding Registers (4x)", wide)
        self.assertIn("Discrete Inputs (1x)", narrow)
        self.assertIn("monitor>", narrow)
        self.assertIn("Recent Changes", compact)
        self.assertIn("monitor>", compact)
        self.assertIn("yellow = recently changed", compact)

    def test_monitor_data_rows_are_unbordered_and_use_terminal_colors(self):
        state = MonitorState(MonitorConfig())
        state.set_status("RUNNING")
        state.record_read_error(DataArea.HOLDING_REGISTERS, "test failure")
        with patch("modbuscli.terminal.supports_color", return_value=True):
            frame = render_monitor(
                state.display(),
                state.config.ranges,
                "localhost",
                502,
                1,
                1,
                "",
                width=100,
                height=20,
            )

        visible_lines = [
            re.sub(r"\x1b\[[0-9;]*m", "", line)
            for line in frame.splitlines()
        ]
        log_index = next(
            index for index, line in enumerate(visible_lines) if "Recent Changes" in line
        )
        self.assertFalse(any(line.startswith("+-") for line in visible_lines[:log_index - 1]))
        self.assertTrue(any(line.startswith("+-") for line in visible_lines[log_index - 1:]))
        self.assertIn("Changes observed: 0", frame)
        self.assertIn("white = normal", "\n".join(visible_lines))
        self.assertIn("\x1b[1;91m", frame)
        self.assertIn("\x1b[1;92m", frame)

    def test_screen_refresh_writes_only_changed_rows(self):
        previous = "header\ncoil row\nregister row\nmonitor> \nlegend"
        current = "header\ncoil row changed\nregister row\nmonitor> \nlegend"

        update = update_monitor_screen(current, previous)

        self.assertNotIn("\033[2J", update)
        self.assertIn("\033[2;1H\033[2Kcoil row changed", update)
        self.assertNotIn("register row", update)
        self.assertNotIn("legend", update)

    def test_screen_update_does_not_redraw_when_nothing_changed(self):
        frame = "header\nvalues\nmonitor> \nlegend"

        self.assertEqual(update_monitor_screen(frame, frame), "")

    def test_command_feedback_is_rendered_immediately_after_prompt(self):
        state = MonitorState(MonitorConfig())
        frame = render_monitor(
            state.display(),
            state.config.ranges,
            "localhost",
            502,
            1,
            1,
            "",
            notice="Polling paused. Writes remain enabled.",
            width=100,
            height=24,
        )
        lines = frame.splitlines()
        prompt_index = next(
            index for index, line in enumerate(lines) if line.startswith("monitor>")
        )

        self.assertEqual(lines[prompt_index + 1].strip(), "Polling paused. Writes remain enabled.")
        self.assertGreater(
            prompt_index,
            next(index for index, line in enumerate(lines) if "Recent Changes" in line),
        )

    def test_recently_changed_highlight_expires_after_five_seconds(self):
        state = MonitorState(MonitorConfig())
        state.record_read(DataArea.HOLDING_REGISTERS, [123])
        state.record_read(DataArea.HOLDING_REGISTERS, [456])
        now = datetime.now()
        with patch("modbuscli.terminal.supports_color", return_value=True):
            recent_frame = render_monitor(
                state.display(),
                state.config.ranges,
                "localhost",
                502,
                1,
                1,
                "",
                width=120,
                height=30,
                now=now,
            )
            expired_frame = render_monitor(
                state.display(),
                state.config.ranges,
                "localhost",
                502,
                1,
                1,
                "",
                width=120,
                height=30,
                now=now + timedelta(seconds=6),
            )

        def value_row(frame):
            return next(
                line
                for line in frame.splitlines()
                if "456" in re.sub(r"\x1b\[[0-9;]*m", "", line)
            )

        self.assertIn("\x1b[1;93m", value_row(recent_frame))
        self.assertNotIn("\x1b[1;93m", value_row(expired_frame))

    def test_recent_changes_panel_has_its_own_border(self):
        state = MonitorState(MonitorConfig())
        state._events.append(
            MonitorEvent(datetime.now(), "CHANGE", "HR[0] 123 -> 999")
        )
        frame = render_monitor(
            state.display(),
            state.config.ranges,
            "localhost",
            502,
            1,
            1,
            "",
            width=120,
            height=30,
        )
        visible = [
            re.sub(r"\x1b\[[0-9;]*m", "", line)
            for line in frame.splitlines()
        ]
        title_index = next(
            index for index, line in enumerate(visible) if "Recent Changes" in line
        )

        self.assertTrue(visible[title_index - 1].startswith("+-"))
        self.assertTrue(visible[title_index + 1].startswith("+-"))
        self.assertIn("HR[0] 123 -> 999", visible[title_index + 2])

    def test_ongoing_continuous_writes_panel_follows_recent_changes(self):
        state = MonitorState(MonitorConfig())
        state.set_continuous_writes(
            (
                ContinuousWriteDisplay(
                    1, DataArea.COILS, 1, (1,) * 10, None, False
                ),
            )
        )
        frame = render_monitor(
            state.display(),
            state.config.ranges,
            "localhost",
            502,
            1,
            1,
            "",
            width=120,
            height=30,
        )
        visible = [
            re.sub(r"\x1b\[[0-9;]*m", "", line)
            for line in frame.splitlines()
        ]
        recent_index = next(
            index for index, line in enumerate(visible) if "Recent Changes" in line
        )
        ongoing_index = next(
            index
            for index, line in enumerate(visible)
            if "Ongoing Continuous Writes" in line
        )

        self.assertGreater(ongoing_index, recent_index)
        self.assertEqual(
            sum("Ongoing Continuous Writes" in line for line in visible), 1
        )
        self.assertTrue(visible[ongoing_index - 1].startswith("+-"))
        self.assertTrue(visible[ongoing_index + 1].startswith("+-"))
        ongoing_text = "\n".join(visible[ongoing_index:])
        self.assertIn("C[1-10] = ON", ongoing_text)
        self.assertIn("RUNNING", ongoing_text)
        self.assertIn("#1", ongoing_text)
        self.assertIn("pause 1", ongoing_text)
        self.assertIn("stop 1", ongoing_text)

    def test_continuous_write_panel_does_not_overlap_recent_changes_at_height_limit(self):
        state = MonitorState(MonitorConfig())
        state.set_continuous_writes(
            (
                ContinuousWriteDisplay(
                    1, DataArea.COILS, 1, (1,) * 10, 1.0, False
                ),
                ContinuousWriteDisplay(
                    2, DataArea.HOLDING_REGISTERS, 4, (321,), 3.0, True
                ),
            )
        )
        for index in range(4):
            state._events.append(
                MonitorEvent(
                    datetime.now(),
                    "CHANGE",
                    f"HR[{index}] {index} -> {index + 1}",
                )
            )

        frame = render_monitor(
            state.display(),
            state.config.ranges,
            "localhost",
            502,
            1,
            1,
            "",
            width=180,
            height=20,
        )
        visible = [
            re.sub(r"\x1b\[[0-9;]*m", "", line)
            for line in frame.splitlines()
        ]
        recent_index = next(
            index for index, line in enumerate(visible) if "Recent Changes" in line
        )
        ongoing_index = next(
            index
            for index, line in enumerate(visible)
            if "Ongoing Continuous Writes" in line
        )
        recent_bottom = next(
            index
            for index in range(recent_index + 2, ongoing_index)
            if visible[index].startswith("+-")
        )

        self.assertLessEqual(len(visible), 20)
        self.assertTrue(visible[recent_index - 1].startswith("+-"))
        self.assertTrue(visible[recent_bottom].startswith("+-"))
        self.assertEqual(visible[recent_bottom + 1], visible[ongoing_index - 1])
        self.assertIn("#1", visible[ongoing_index + 2])
        self.assertIn("#2", "\n".join(visible[ongoing_index:]))

    def test_main_menu_includes_monitor_and_manage_sessions(self):
        config = MonitorConfig()
        with (
            patch("modbuscli.app._print_banner"),
            patch("modbuscli.app._connection_subtitle", return_value="target"),
            patch("modbuscli.app._show_menu", side_effect=[1, -1]) as show_menu,
            patch("modbuscli.app._monitor_menu", return_value=config),
            patch("modbuscli.app.SessionManager") as manager_class,
        ):
            app.main()

        self.assertEqual(
            show_menu.call_args_list[0].args[0],
            ("Client mode", "Monitor & Interact", "Manage sessions", "Configure connection"),
        )
        manager_class.return_value.stop_all.assert_called_once()

    def test_monitor_configuration_prompts_for_all_ranges(self):
        config = MonitorConfig()
        answers = []
        for index in range(4):
            answers.extend((index * 10, 20))
        with (
            patch("modbuscli.app._prompt_int", side_effect=answers),
            patch("modbuscli.app._prompt_interval", return_value=0.5),
        ):
            configured = app._configure_monitor(config)

        self.assertEqual(configured.interval, 0.5)
        self.assertEqual(
            [configured.ranges[area] for area in DATA_AREAS],
            [MonitorRange(index * 10, 20) for index in range(4)],
        )


if __name__ == "__main__":
    unittest.main()
