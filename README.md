# Modbus CLI

An interactive Modbus TCP client for reading and writing coils and registers.
Periodic reads run in a focused screen, while periodic writes can run as
background sessions.

## Requirements

- Python 3.10 or later
- A reachable Modbus TCP endpoint

## Install and run

```powershell
py -m pip install -e .
modbuscli
```

Or run from the project directory without installing the command:

```powershell
py -m modbuscli
```

The default target is `127.0.0.1:502` with unit ID `1`. Use **Configure
connection** from the main menu to change the target, port, or unit ID. Return
to the main menu to change the connection. The terminal is cleared when
entering client mode. Menus use a bordered selection interface: enter the
number shown beside an option, then press Enter. Each menu provides an Exit or
Back option. The main and client menus show a TCP connection check for the
configured host and port; the check is repeated whenever a menu is displayed.
The probe briefly opens and closes a TCP connection, so it indicates current
reachability rather than a persistent connection. The client supports Modbus
TCP.

## Operations

- Data-area selections use the standard Modbus reference prefixes: coils (0x),
  discrete inputs (1x), input registers (3x), and holding registers (4x).
- Single reads and writes open a dedicated result screen with options to repeat
  using the same settings, run again with new settings, or return to Client
  mode. For writes, changing settings keeps the Single write screen open while
  you choose an area and enter the new address and values.
- Read coils, discrete inputs, holding registers, or input registers once.
- Write one or more coils or holding registers once or periodically. For
  multiple consecutive values, enter comma-separated values (for example,
  `1,0,1` for coils or `123,456` for holding registers).
- Periodic reads run in a dedicated screen with live values and Pause/Resume
  and Cancel controls. The client menu returns after the read is cancelled.
- Periodic writes run in the background, so you can read from the device while
  a write session continues. Background writes can be paused, resumed, or
  stopped from the session manager.
- List active background sessions to inspect their state, cycle count, latest
  result, or latest error; stopped and failed sessions are not shown. Stop one
  session or all sessions from the session manager. Session IDs are reused
  once a session stops or fails, so IDs stay compact among active sessions.

Addresses use the zero-based offsets expected by pymodbus. Coil values must be
`0` or `1`; register values must be between `0` and `65535`. Periodic intervals
must be at least 0.1 seconds. Sessions run only while the application remains
open, and all running sessions are stopped when you exit.

## Tests

```powershell
py -m unittest discover -s tests -v
```
