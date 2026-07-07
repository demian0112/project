from __future__ import annotations

import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from statistics import mean

from .csi_payload_service import CsiBatch


@dataclass(frozen=True, slots=True)
class CsiPoint:
    batch_no: int
    seq0: int
    seq1: int
    frame_count: int
    invalid_count: int
    ts0: int
    received_at: datetime


@dataclass(frozen=True, slots=True)
class CsiQualityResult:
    quality: str
    seq_reset: bool = False
    previous_seq1: int | None = None
    new_seq0: int | None = None
    gap_seconds: float | None = None


class CsiQualityTracker:
    """Estimate CSI quality from real frame ranges over a rolling window."""

    def __init__(
        self,
        max_points: int = 10,
        *,
        expected_interval_seconds: float = 1.5,
        soft_timeout_seconds: float = 8.0,
        recovery_grace_seconds: float = 20.0,
        seq_reset_max: int = 5,
    ) -> None:
        self._points: dict[tuple[str, str], deque[CsiPoint]] = defaultdict(
            lambda: deque(maxlen=max_points)
        )
        self._lock = threading.RLock()
        self.expected_interval_seconds = expected_interval_seconds
        self.soft_timeout_seconds = soft_timeout_seconds
        self.recovery_grace_seconds = recovery_grace_seconds
        self.normal_interval_seconds = max(3.0, expected_interval_seconds * 2)
        self.seq_reset_max = seq_reset_max

    def add(
        self,
        device_name: str,
        session: str,
        batch: CsiBatch,
        received_at: datetime,
    ) -> CsiQualityResult:
        key = (device_name, session)
        with self._lock:
            points = self._points[key]
            point = CsiPoint(
                batch_no=batch.batch_no,
                seq0=batch.seq0,
                seq1=batch.seq1,
                frame_count=batch.frame_count,
                invalid_count=batch.invalid_count,
                ts0=batch.ts0,
                received_at=received_at,
            )
            seq_reset = False
            previous_seq1 = None
            gap_seconds = None
            if points:
                previous = points[-1]
                gap_seconds = _point_interval(previous, point)
                if batch.seq0 <= previous.seq1 and batch.seq0 <= self.seq_reset_max:
                    seq_reset = True
                    previous_seq1 = previous.seq1
                    points.clear()

            points.append(point)
            if seq_reset:
                return CsiQualityResult(
                    quality="fair",
                    seq_reset=True,
                    previous_seq1=previous_seq1,
                    new_seq0=batch.seq0,
                    gap_seconds=gap_seconds,
                )
            return CsiQualityResult(
                quality=_quality(
                    list(points),
                    normal_interval_seconds=self.normal_interval_seconds,
                    soft_timeout_seconds=self.soft_timeout_seconds,
                    recovery_grace_seconds=self.recovery_grace_seconds,
                )
            )

    def clear(self, device_name: str, session: str | None = None) -> None:
        with self._lock:
            if session is not None:
                self._points.pop((device_name, session), None)
                return
            for key in [
                key for key in self._points if key[0] == device_name
            ]:
                self._points.pop(key, None)


def _quality(
    points: list[CsiPoint],
    *,
    normal_interval_seconds: float,
    soft_timeout_seconds: float,
    recovery_grace_seconds: float,
) -> str:
    if len(points) < 2:
        return "unknown"

    received_frames = sum(point.frame_count for point in points)
    invalid_frames = sum(point.invalid_count for point in points)
    missing_frames = 0
    non_monotonic = False

    for point in points:
        sequence_span = point.seq1 - point.seq0 + 1
        if sequence_span < point.frame_count:
            non_monotonic = True
        else:
            missing_frames += sequence_span - point.frame_count

    intervals: list[float] = []
    for previous, current in zip(points, points[1:]):
        sequence_gap = current.seq0 - previous.seq1 - 1
        if sequence_gap < 0 or current.batch_no <= previous.batch_no:
            non_monotonic = True
        else:
            missing_frames += sequence_gap
        intervals.append(_point_interval(previous, current))

    valid_intervals = [value for value in intervals if value > 0]
    if not valid_intervals or received_frames <= 0:
        return "poor"

    expected_frames = received_frames + missing_frames
    loss_rate = missing_frames / max(1, expected_frames)
    invalid_rate = invalid_frames / received_frames
    average_interval = mean(valid_intervals)
    max_interval = max(valid_intervals)
    unstable_intervals = sum(
        value < 0.4 or value > soft_timeout_seconds
        for value in valid_intervals
    )
    stretched_intervals = sum(
        normal_interval_seconds < value <= soft_timeout_seconds
        for value in valid_intervals
    )

    if (
        not non_monotonic
        and loss_rate <= 0.05
        and invalid_rate <= 0.05
        and unstable_intervals == 0
        and stretched_intervals == 0
    ):
        return "good"
    if (
        not non_monotonic
        and loss_rate <= 0.20
        and invalid_rate <= 0.15
        and average_interval <= soft_timeout_seconds
        and max_interval <= soft_timeout_seconds
        and unstable_intervals <= max(1, len(valid_intervals) // 3)
    ):
        return "fair"
    if max_interval <= recovery_grace_seconds:
        return "poor"
    return "poor"


def _point_interval(previous: CsiPoint, current: CsiPoint) -> float:
    device_interval = (current.ts0 - previous.ts0) / 1_000_000
    if device_interval > 0:
        return device_interval
    return (current.received_at - previous.received_at).total_seconds()
