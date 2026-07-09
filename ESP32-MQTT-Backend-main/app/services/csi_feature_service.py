from __future__ import annotations

import math
from collections.abc import Sequence


def _to_signed_int8(value: int) -> int:
    parsed = int(value)
    if -128 <= parsed <= 127:
        return parsed
    if 128 <= parsed <= 255:
        return parsed - 256
    raise ValueError("CSI I/Q byte is outside int8 range")


def raw_iq_to_amplitude(raw_csi: bytes | bytearray | memoryview | Sequence[int]) -> list[float]:
    """Convert interleaved int8 I/Q CSI bytes into per-subcarrier amplitude."""
    if not raw_csi:
        return []

    count = len(raw_csi) - (len(raw_csi) % 2)
    amplitudes: list[float] = []
    for index in range(0, count, 2):
        i_value = _to_signed_int8(raw_csi[index])
        q_value = _to_signed_int8(raw_csi[index + 1])
        amplitudes.append(math.hypot(i_value, q_value))
    return amplitudes
