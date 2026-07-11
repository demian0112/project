from __future__ import annotations

import json
from csv import writer
from dataclasses import dataclass
from io import StringIO
from typing import Any

from .csi_payload_service import CsiFrame


class CsiAlgorithmFormatError(ValueError):
    """Raised when a decoded CSI frame cannot be formatted for Docker."""


@dataclass(frozen=True, slots=True)
class CsiAlgorithmFormatterConfig:
    csi_type: str = "CSI_DATA"
    rate_sig_mode: int = 11
    channel: int = 100
    fft_gain: int = 0
    agc_gain: int = 0
    rx_state: int = 0
    secondary_channel: int = 0
    noise_floor: int = 0
    payload_type: int = 13
    max_raw_csi_bytes: int = 4096

    @classmethod
    def from_app_config(cls, config: dict[str, Any]) -> "CsiAlgorithmFormatterConfig":
        return cls(
            csi_type=str(config.get("FALL_ALGORITHM_CSI_TYPE") or "CSI_DATA"),
            rate_sig_mode=int(config.get("FALL_ALGORITHM_RATE_SIG_MODE", 11)),
            channel=int(config.get("FALL_ALGORITHM_CHANNEL", 100)),
            fft_gain=int(config.get("FALL_ALGORITHM_FFT_GAIN", 0)),
            agc_gain=int(config.get("FALL_ALGORITHM_AGC_GAIN", 0)),
            rx_state=int(config.get("FALL_ALGORITHM_RX_STATE", 0)),
            secondary_channel=int(
                config.get("FALL_ALGORITHM_SECONDARY_CHANNEL", 0)
            ),
            noise_floor=int(config.get("FALL_ALGORITHM_NOISE_FLOOR", 0)),
            payload_type=int(config.get("FALL_ALGORITHM_PAYLOAD_TYPE", 13)),
        )


class CsiAlgorithmFormatter:
    """Convert decoded CSIB frames into the Docker CSV/WebSocket contract.

    Mapping:
    - Docker `ID` is the decoded CSIB frame sequence.
    - Docker `MAC` is a stable backend device identity. The current csib64-v2
      payload does not carry the ESP32 physical MAC, so `device.device_name` is
      used consistently and never regenerated per frame.
    - Fields absent from csib64-v2 are centralized in
      CsiAlgorithmFormatterConfig and app config instead of being scattered as
      magic numbers.
    """

    def __init__(self, config: CsiAlgorithmFormatterConfig | None = None) -> None:
        self.config = config or CsiAlgorithmFormatterConfig()

    def to_csv_line(self, frame: CsiFrame, *, device_identity: str) -> str:
        raw = bytes(frame.raw_csi or b"")
        if len(raw) > self.config.max_raw_csi_bytes:
            raise CsiAlgorithmFormatError("raw CSI frame is too large")
        signed = [byte if byte < 128 else byte - 256 for byte in raw]
        csi_json = json.dumps(signed, separators=(",", ":"))
        first_word_invalid = 1 if frame.first_word_invalid else 0
        csi_len = len(raw)
        fields = [
            self.config.csi_type,
            int(frame.sequence),
            str(device_identity),
            int(frame.rssi),
            self.config.rate_sig_mode,
            self.config.noise_floor,
            self.config.fft_gain,
            self.config.agc_gain,
            self.config.channel,
            int(frame.timestamp_us),
            csi_len,
            self.config.rx_state,
            csi_len,
            first_word_invalid,
            csi_json,
        ]
        output = StringIO()
        csv_writer = writer(output, lineterminator="")
        csv_writer.writerow(fields)
        return output.getvalue()

    def to_websocket_message(self, frame: CsiFrame, *, device_identity: str) -> dict:
        return {
            "type": "data",
            "line": self.to_csv_line(frame, device_identity=device_identity),
        }
