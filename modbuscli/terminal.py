import os
import sys


def supports_color() -> bool:
    return (
        sys.stdout.isatty()
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM", "").lower() != "dumb"
    )


def paint(
    text: str, color: str, bold: bool = False, enabled: bool | None = None
) -> str:
    if not (supports_color() if enabled is None else enabled):
        return text
    style = f"\033[{1 if bold else 0};{color}m"
    return f"{style}{text}\033[0m"
