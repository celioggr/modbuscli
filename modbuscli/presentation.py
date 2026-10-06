from collections.abc import Sequence

from .operations import DataArea


_REFERENCE_PREFIX = {
    DataArea.COILS: "0",
    DataArea.DISCRETE_INPUTS: "1",
    DataArea.INPUT_REGISTERS: "3",
    DataArea.HOLDING_REGISTERS: "4",
}


def format_read_table(
    area: DataArea,
    address: int,
    values: Sequence[int] | None,
    count: int | None = None,
    offset_start: int = 0,
) -> str:
    if values is None:
        if count is None:
            raise ValueError("Count is required when formatting pending read values.")
        row_count = count
    else:
        row_count = max(len(values), count or 0)

    last_offset = offset_start + row_count - 1
    offset_width = max(len("Offset"), len(str(max(0, last_offset))))
    if area in (DataArea.COILS, DataArea.DISCRETE_INPUTS):
        address_width = max(7, len("Address"))
        state_width = len("State")
        header = (
            f"{'Offset':<{offset_width}}   "
            f"{'Address':<{address_width}}   "
            f"{'State':<{state_width}}"
        )
        separator = (
            f"{'-' * offset_width}   "
            f"{'-' * address_width}   "
            f"{'-' * state_width}"
        )
        rows = []
        for row_index in range(row_count):
            offset = offset_start + row_index
            address_ref = f"{_REFERENCE_PREFIX[area]}{address + offset + 1:05d}"
            state = (
                ("ON" if values[row_index] else "OFF")
                if values is not None and row_index < len(values)
                else "..."
            )
            rows.append(
                f"{offset:<{offset_width}}   "
                f"{address_ref:<{address_width}}   "
                f"{state:<{state_width}}"
            )
    else:
        decimal_width = max(
            5,
            max((len(str(value)) for value in values or ()), default=0),
        )
        hex_width = len("0x0000")
        header = (
            f"{'Offset':<{offset_width}}   "
            f"{'Modbus Ref':<10}   "
            f"{'Dec':<{decimal_width}}   "
            f"{'Hex':<{hex_width}}"
        )
        separator = (
            f"{'-' * offset_width}   "
            f"{'-' * 10}   "
            f"{'-' * decimal_width}   "
            f"{'-' * hex_width}"
        )
        rows = []
        for row_index in range(row_count):
            offset = offset_start + row_index
            address_ref = f"{_REFERENCE_PREFIX[area]}{address + offset + 1:05d}"
            if values is None or row_index >= len(values):
                decimal_value, hexadecimal_value = "...", "..."
            else:
                value = values[row_index]
                decimal_value, hexadecimal_value = str(value), f"0x{value:04X}"
            rows.append(
                f"{offset:<{offset_width}}   "
                f"{address_ref:<10}   "
                f"{decimal_value:>{decimal_width}}   "
                f"{hexadecimal_value:>{hex_width}}"
            )

    return "\n".join(
        [area.value.title(), "", header, separator, *rows]
    )
