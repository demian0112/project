import pytest

from app.services.csi_algorithm_formatter import (
    CsiAlgorithmFormatError,
    CsiAlgorithmFormatter,
    CsiAlgorithmFormatterConfig,
)
from app.services.csi_payload_service import CsiFrame


def test_csi_frame_formats_exact_docker_csv_and_json_message():
    frame = CsiFrame(
        sequence=52,
        timestamp_us=31_032_532,
        rssi=-30,
        first_word_invalid=True,
        raw_csi=bytes([251, 3, 127, 128]),
    )
    formatter = CsiAlgorithmFormatter()

    line = formatter.to_csv_line(frame, device_identity="esp32-001")

    assert line == (
        'CSI_DATA,52,esp32-001,-30,11,0,0,0,100,31032532,'
        '4,0,4,1,"[-5,3,127,-128]"'
    )
    assert formatter.to_websocket_message(
        frame,
        device_identity="esp32-001",
    ) == {"type": "data", "line": line}


def test_csi_formatter_handles_empty_array_and_custom_defaults():
    frame = CsiFrame(
        sequence=1,
        timestamp_us=2,
        rssi=-45,
        first_word_invalid=False,
        raw_csi=b"",
    )
    formatter = CsiAlgorithmFormatter(
        CsiAlgorithmFormatterConfig(
            csi_type="TYPE",
            rate_sig_mode=7,
            channel=6,
            fft_gain=1,
            agc_gain=2,
            noise_floor=-95,
            rx_state=99,
        )
    )

    assert formatter.to_csv_line(frame, device_identity="stable-device") == (
        'TYPE,1,stable-device,-45,7,-95,1,2,6,2,0,99,0,0,[]'
    )


def test_csi_formatter_rejects_abnormally_large_raw_csi():
    frame = CsiFrame(
        sequence=1,
        timestamp_us=2,
        rssi=-45,
        first_word_invalid=False,
        raw_csi=b"\x00" * 5,
    )
    formatter = CsiAlgorithmFormatter(
        CsiAlgorithmFormatterConfig(max_raw_csi_bytes=4)
    )

    with pytest.raises(CsiAlgorithmFormatError):
        formatter.to_csv_line(frame, device_identity="device")
