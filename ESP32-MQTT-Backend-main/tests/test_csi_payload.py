import base64
import struct

import pytest

from app.services.csi_payload_service import (
    BATCH_HEADER,
    FRAME_HEADER,
    CsiPayloadError,
    decode_csi_payload,
)


def build_csi_payload(
    *,
    session="sess-test-001",
    batch_no=1,
    sequences=(1, 2),
    invalid=(False, False),
    sample_hz=30,
):
    timestamps = tuple(1_000_000 + index * 33_333 for index in range(len(sequences)))
    parts = [
        BATCH_HEADER.pack(
            b"CSIB",
            0x01,
            len(sequences),
            sample_hz,
            0,
            batch_no,
            timestamps[0],
            timestamps[-1],
        )
    ]
    for sequence, timestamp, is_invalid in zip(
        sequences,
        timestamps,
        invalid,
    ):
        raw_csi = bytes([sequence % 128, 2, 3, 4])
        parts.append(
            FRAME_HEADER.pack(
                sequence,
                timestamp,
                -45,
                int(is_invalid),
                len(raw_csi),
            )
        )
        parts.append(raw_csi)
    binary = b"".join(parts)
    return {
        "session": session,
        "batch": batch_no,
        "frames": len(sequences),
        "seq0": sequences[0],
        "seq1": sequences[-1],
        "ts0": timestamps[0],
        "ts1": timestamps[-1],
        "fmt": "csib64-v2",
        "bytes": len(binary),
        "data": base64.b64encode(binary).decode("ascii"),
        "ts": 240,
    }


def test_decode_current_hardware_csi_batch():
    payload = build_csi_payload(invalid=(False, True))

    batch = decode_csi_payload(payload)

    assert batch.session == "sess-test-001"
    assert batch.batch_no == 1
    assert batch.sample_hz == 30
    assert batch.frame_count == 2
    assert batch.average_rssi == -45
    assert batch.invalid_count == 1
    assert batch.frames[0].sequence == 1
    assert batch.frames[1].first_word_invalid is True
    assert batch.frames[0].raw_csi == b"\x01\x02\x03\x04"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("fmt", "csib64-v1", "unsupported CSI format"),
        ("frames", 3, "frames does not match"),
        ("bytes", 999, "bytes does not match"),
        ("seq1", 99, "seq0/seq1"),
        ("ts0", 99, "ts0/ts1"),
    ],
)
def test_decode_rejects_json_binary_mismatches(field, value, message):
    payload = build_csi_payload()
    payload[field] = value

    with pytest.raises(CsiPayloadError, match=message):
        decode_csi_payload(payload)


def test_decode_rejects_trailing_or_invalid_base64_data():
    payload = build_csi_payload()
    payload["data"] = "not base64!"

    with pytest.raises(CsiPayloadError, match="Base64"):
        decode_csi_payload(payload)


def test_frame_layout_matches_hardware_document():
    assert BATCH_HEADER.size == struct.calcsize("<4sBBBBIqq") == 28
    assert FRAME_HEADER.size == struct.calcsize("<IqbBH") == 16
