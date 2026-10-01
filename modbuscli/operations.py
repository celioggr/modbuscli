from enum import Enum
from typing import Any


class DataArea(str, Enum):
    COILS = "coils"
    DISCRETE_INPUTS = "discrete inputs"
    INPUT_REGISTERS = "input registers"
    HOLDING_REGISTERS = "holding registers"

    @property
    def menu_label(self) -> str:
        return {
            DataArea.COILS: "Coils (0x)",
            DataArea.DISCRETE_INPUTS: "Discrete Inputs (1x)",
            DataArea.INPUT_REGISTERS: "Input Registers (3x)",
            DataArea.HOLDING_REGISTERS: "Holding Registers (4x)",
        }[self]


def validate_address_count(area: DataArea, address: int, count: int) -> None:
    if not 0 <= address <= 65535:
        raise ValueError("Address must be between 0 and 65535.")
    if count < 1:
        raise ValueError("Count must be at least 1.")
    max_count = 2000 if area in (DataArea.COILS, DataArea.DISCRETE_INPUTS) else 125
    if count > max_count:
        raise ValueError(f"Read count cannot exceed {max_count} for {area.value}.")
    if address + count > 65536:
        raise ValueError("Address plus count cannot exceed 65536.")


def validate_write_values(area: DataArea, address: int, values: list[int]) -> None:
    if area not in (DataArea.COILS, DataArea.HOLDING_REGISTERS):
        raise ValueError("Only coils and holding registers can be written.")
    if not 0 <= address <= 65535:
        raise ValueError("Address must be between 0 and 65535.")
    if not values:
        raise ValueError("Enter at least one value.")
    max_count = 1968 if area == DataArea.COILS else 123
    if len(values) > max_count:
        raise ValueError(f"Write count cannot exceed {max_count} for {area.value}.")
    if address + len(values) > 65536:
        raise ValueError("Address plus value count cannot exceed 65536.")
    max_value = 1 if area == DataArea.COILS else 65535
    if any(value < 0 or value > max_value for value in values):
        if area == DataArea.COILS:
            raise ValueError("Coil values must be 0 or 1.")
        raise ValueError("Register values must be between 0 and 65535.")


def read_values(client: Any, area: DataArea, address: int, count: int, unit_id: int) -> list[int]:
    validate_address_count(area, address, count)
    method_name = {
        DataArea.COILS: "read_coils",
        DataArea.DISCRETE_INPUTS: "read_discrete_inputs",
        DataArea.HOLDING_REGISTERS: "read_holding_registers",
        DataArea.INPUT_REGISTERS: "read_input_registers",
    }[area]
    response = getattr(client, method_name)(address, count=count, device_id=unit_id)
    _raise_on_modbus_error(response)
    if area in (DataArea.COILS, DataArea.DISCRETE_INPUTS):
        return [int(value) for value in response.bits[:count]]
    return list(response.registers)


def write_values(client: Any, area: DataArea, address: int, values: list[int], unit_id: int) -> None:
    validate_write_values(area, address, values)
    if area == DataArea.COILS:
        if len(values) == 1:
            response = client.write_coil(address, bool(values[0]), device_id=unit_id)
        else:
            response = client.write_coils(address, [bool(value) for value in values], device_id=unit_id)
    elif len(values) == 1:
        response = client.write_register(address, values[0], device_id=unit_id)
    else:
        response = client.write_registers(address, values, device_id=unit_id)
    _raise_on_modbus_error(response)


def _raise_on_modbus_error(response: Any) -> None:
    if response.isError():
        raise RuntimeError(f"Modbus error response: {response}")
