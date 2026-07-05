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


class CsiQualityTracker:
    """Estimate CSI quality from real frame ranges over a rolling window."""

    def __init__(self, max_points: int = 10) -> None:
        self._points: dict[tuple[str, str], deque[CsiPoint]] = defaultdict(
            lambda: deque(maxlen=max_points)
        )
        self._lock = threading.RLock()

    def add(
        self,
        device_name: str,
        session: str,
        batch: CsiBatch,
        received_at: datetime,
    ) -> str:
        key = (device_name, session)
        with self._lock:
            points = self._points[key]
            points.append(
                CsiPoint(
                    batch_no=batch.batch_no,
                    seq0=batch.seq0,
                    seq1=batch.seq1,
                    frame_count=batch.frame_count,
                    invalid_count=batch.invalid_count,
                    ts0=batch.ts0,
                    received_at=received_at,
                )
            )
            return _quality(list(points))

    def clear(self, device_name: str, session: str | None = None) -> None:
        with self._lock:
            if session is not None:
                self._points.pop((device_name, session), None)
                return
            for key in [
                key for key in self._points if key[0] == device_name
            ]:
                self._points.pop(key, None)


def _quality(points: list[CsiPoint]) -> str:
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
        device_interval = (current.ts0 - previous.ts0) / 1_000_000
        intervals.append(
            device_interval
            if device_interval > 0
            else (
                current.received_at - previous.received_at
            ).total_seconds()
        )

    valid_intervals = [value for value in intervals if value > 0]
    if not valid_intervals or received_frames <= 0:
        return "poor"

    expected_frames = received_frames + missing_frames
    loss_rate = missing_frames / max(1, expected_frames)
    invalid_rate = invalid_frames / received_frames
    average_interval = mean(valid_intervals)
    unstable_intervals = sum(
        value < 0.4 or value > 2.5 for value in valid_intervals
    )

    if (
        not non_monotonic
        and loss_rate <= 0.02
        and invalid_rate <= 0.02
        and unstable_intervals == 0
    ):
        return "good"
    if (
        not non_monotonic
        and loss_rate <= 0.10
        and invalid_rate <= 0.10
        and average_interval <= 4
        and unstable_intervals <= max(1, len(valid_intervals) // 3)
    ):
        return "fair"
    return "poor"
