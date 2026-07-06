from __future__ import annotations

import base64
import binascii
import struct
from dataclasses import dataclass
from typing import Any


# C板CSI上传MQTT数据格式说明.md defines the current CSIB v2 batch as a
# 28-byte batch header followed by 16-byte frame headers.
BATCH_HEADER = struct.Struct("<4sBBBBIqq")
FRAME_HEADER = struct.Struct("<IqbBH")
SUPPORTED_FORMAT = "csib64-v2"
SUPPORTED_MAGIC = b"CSIB"
SUPPORTED_BINARY_VERSION = 0x01
MAX_BATCH_FRAMES = 80
MAX_BINARY_BYTES = 512 * 1024
MAX_FRAME_BYTES = 384


class CsiPayloadError(ValueError):
    """The MQTT JSON or its embedded CSI binary batch is invalid."""


@dataclass(frozen=True, slots=True)
class CsiFrame:
    sequence: int
    timestamp_us: int
    rssi: int
    first_word_invalid: bool
    raw_csi: bytes


@dataclass(frozen=True, slots=True)
class CsiBatch:
    session: str
    batch_no: int
    sample_hz: int
    frames: tuple[CsiFrame, ...]
    seq0: int
    seq1: int
    ts0: int
    ts1: int
    average_rssi: int
    invalid_count: int
    binary_bytes: int

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    def to_algorithm_input(self) -> dict[str, Any]:
        """Return decoded, in-memory-only data for the future fall algorithm."""
        return {
            "session": self.session,
            "batch": self.batch_no,
            "sample_hz": self.sample_hz,
            "seq0": self.seq0,
            "seq1": self.seq1,
            "ts0": self.ts0,
            "ts1": self.ts1,
            "rssi": self.average_rssi,
            "invalid": self.invalid_count,
            "frames": [
                {
                    "seq": frame.sequence,
                    "timestamp_us": frame.timestamp_us,
                    "rssi": frame.rssi,
                    "first_word_invalid": frame.first_word_invalid,
                    "raw_csi": frame.raw_csi,
                }
                for frame in self.frames
            ],
        }


def decode_csi_payload(payload: dict[str, Any]) -> CsiBatch:
    """Decode and cross-check a ``csib64-v2`` CSIB MQTT payload."""
    session = str(payload.get("session") or "").strip()
    if not session:
        raise CsiPayloadError("session is required")
    if payload.get("fmt") != SUPPORTED_FORMAT:
        raise CsiPayloadError("unsupported CSI format")

    batch_no = _required_int(payload, "batch", minimum=0)
    frame_count = _required_int(
        payload,
        "frames",
        minimum=1,
        maximum=MAX_BATCH_FRAMES,
    )
    seq0 = _required_int(payload, "seq0", minimum=0)
    seq1 = _required_int(payload, "seq1", minimum=seq0)
    ts0 = _required_int(payload, "ts0", minimum=0)
    ts1 = _required_int(payload, "ts1", minimum=ts0)
    binary_bytes = _required_int(
        payload,
        "bytes",
        minimum=BATCH_HEADER.size + FRAME_HEADER.size,
        maximum=MAX_BINARY_BYTES,
    )
    encoded = payload.get("data")
    if not isinstance(encoded, str) or not encoded:
        raise CsiPayloadError("data must be a non-empty Base64 string")
    if len(encoded) > ((MAX_BINARY_BYTES + 2) // 3) * 4:
        raise CsiPayloadError("encoded CSI batch is too large")
    try:
        binary = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CsiPayloadError("data is not valid Base64") from exc
    if len(binary) != binary_bytes:
        raise CsiPayloadError("bytes does not match decoded data length")

    (
        magic,
        version,
        binary_frame_count,
        sample_hz,
        reserved,
        binary_batch_no,
        start_ts_us,
        end_ts_us,
    ) = BATCH_HEADER.unpack_from(binary, 0)
    if magic != SUPPORTED_MAGIC:
        raise CsiPayloadError("invalid CSI batch magic")
    if version != SUPPORTED_BINARY_VERSION:
        raise CsiPayloadError("unsupported CSI binary version")
    if binary_frame_count != frame_count:
        raise CsiPayloadError("frames does not match binary frame count")
    if sample_hz <= 0:
        raise CsiPayloadError("sample_hz must be positive")
    if reserved != 0:
        raise CsiPayloadError("reserved batch header field must be 0")
    if binary_batch_no != batch_no:
        raise CsiPayloadError("batch does not match binary batch number")
    if start_ts_us != ts0 or end_ts_us != ts1:
        raise CsiPayloadError("ts0/ts1 do not match batch header")

    offset = BATCH_HEADER.size
    frames: list[CsiFrame] = []
    for _ in range(binary_frame_count):
        if offset + FRAME_HEADER.size > len(binary):
            raise CsiPayloadError("truncated CSI frame header")
        sequence, timestamp_us, rssi, first_word_invalid, csi_len = (
            FRAME_HEADER.unpack_from(binary, offset)
        )
        offset += FRAME_HEADER.size
        if csi_len <= 0 or csi_len > MAX_FRAME_BYTES:
            raise CsiPayloadError("invalid CSI frame length")
        if first_word_invalid not in {0, 1}:
            raise CsiPayloadError("invalid first_word_invalid flag")
        end = offset + csi_len
        if end > len(binary):
            raise CsiPayloadError("truncated CSI frame payload")
        frames.append(
            CsiFrame(
                sequence=sequence,
                timestamp_us=timestamp_us,
                rssi=rssi,
                first_word_invalid=bool(first_word_invalid),
                raw_csi=binary[offset:end],
            )
        )
        offset = end

    if offset != len(binary):
        raise CsiPayloadError("unexpected trailing bytes in CSI batch")
    if any(
        current.sequence <= previous.sequence
        for previous, current in zip(frames, frames[1:])
    ):
        raise CsiPayloadError("CSI frame sequences are not increasing")
    if any(
        current.timestamp_us < previous.timestamp_us
        for previous, current in zip(frames, frames[1:])
    ):
        raise CsiPayloadError("CSI frame timestamps are not increasing")
    if frames[0].sequence != seq0 or frames[-1].sequence != seq1:
        raise CsiPayloadError("seq0/seq1 do not match decoded frames")
    if frames[0].timestamp_us != ts0 or frames[-1].timestamp_us != ts1:
        raise CsiPayloadError("ts0/ts1 do not match decoded frames")
    average_rssi = round(sum(frame.rssi for frame in frames) / len(frames))
    invalid_count = sum(frame.first_word_invalid for frame in frames)

    return CsiBatch(
        session=session,
        batch_no=batch_no,
        sample_hz=sample_hz,
        frames=tuple(frames),
        seq0=seq0,
        seq1=seq1,
        ts0=ts0,
        ts1=ts1,
        average_rssi=average_rssi,
        invalid_count=invalid_count,
        binary_bytes=binary_bytes,
    )


def _required_int(
    payload: dict[str, Any],
    name: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    value = payload.get(name)
    if isinstance(value, bool):
        raise CsiPayloadError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise CsiPayloadError(f"{name} must be an integer") from exc
    if parsed < minimum or (maximum is not None and parsed > maximum):
        raise CsiPayloadError(f"{name} is outside the supported range")
    return parsed
