# Modbus CLI

An interactive Modbus TCP client for reading and writing coils and registers.

## Dependencies

- Python 3.10 or later
- `console-menu` 0.8.x
- `pymodbus` 3.x

Dependencies are installed automatically with the package.

## Run

From the project directory:

```powershell
py -m pip install .
modbuscli
```

Or run directly from the source tree:

```powershell
py -m modbuscli
```

The default connection is `127.0.0.1:502`, Unit ID `1`. Change it in the
Configure connection menu.

## Compile

```powershell
py -m compileall -q modbuscli
```
