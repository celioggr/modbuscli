# Modbus CLI

An interactive Modbus TCP client for reading and writing coils and registers.

## Dependencies

- Python 3.10 or later
- `console-menu` 0.8.x
- `pymodbus` 3.x

Dependencies are installed automatically with the package.

## Implemented Features

The Modbus CLI supports the most commonly used Modbus functions for reading and writing standard Modbus data types. Addressing can be specified using the standard Modbus reference prefixes:

- **0x** - Coils
- **1x** - Discrete Inputs
- **3x** - Input Registers
- **4x** - Holding Registers

### Supported Modbus Functions

| Function Code | Name | Description |
|--------------|------|-------------|
| 0x01 | Read Coils | Read one or more coils (0x) |
| 0x02 | Read Discrete Inputs | Read one or more discrete inputs (1x) |
| 0x03 | Read Holding Registers | Read one or more holding registers (4x) |
| 0x04 | Read Input Registers | Read one or more input registers (3x) |
| 0x05 | Write Single Coil | Write a single coil (0x) |
| 0x06 | Write Single Register | Write a single holding register (4x) |
| 0x0F | Write Multiple Coils | Write multiple coils (0x) |
| 0x10 | Write Multiple Registers | Write multiple holding registers (4x) |

### Concurrent Sessions

The tool supports running multiple Modbus sessions in parallel. This allows different operations to execute simultaneously, for example:

- Continuously writing to a coil
- Monitoring coil values from another session to observe state changes in real time.
- Running independent read and write operations against the same or different devices.
 
This makes it possible to simulate device behavior, test automation logic, and monitor changes without interrupting other active sessions.

### Monitor & Interact

**Monitor & Interact** gives you a live view of the Modbus data areas while polling continues in the background. This is especially useful when exploring more complex systems, as you can quickly see **which values are changing and how the system responds when you interact with it**.

The monitor displays 10 values from each area by default, with configurable offsets, ranges, and polling intervals. Changes are highlighted and added to a recent-event history.

From the `monitor>` prompt, you can perform one-time, range, and continuous writes to coils and holding registers:

```text
hr 0=999
c 2=off
c 1-10=on
repeat hr 4=321
repeat c 1-10=on every 0.5s
pause 1
resume 1
stop 1
pause
resume
clear
status
help
quit
```

`hr` targets holding registers and `c` targets coils. Ranges are inclusive, so `c 1-10=on` writes ten coils. Continuous writes can be paused, resumed, or stopped individually using their ID, while `pause` and `resume` without an ID control monitor polling.

**Client mode** remains available for standard individual and periodic read/write operations.



## Screenshots 
![](./images/modbuscli01.png)
 
![](./images/modbuscli02.png)
 
![](./images/modbuscli03.png)
 
![](./images/modbuscli04.png)
 
![](./images/modbuscli05.png)

![](./images/modbuscli06.png)

## Run

From the source tree using virtual envs:

```powershell
python3 -m venv .venv
source .venv/bin/activate
pip3 install -r requirements.txt
python3 -m modbuscli
```

The default connection is `127.0.0.1:502`, Unit ID `1`. Change it in the
Configure connection menu.


## To Do
- Scanning for active mobbus unit IDs 
- Scanning for modbus devices on the network