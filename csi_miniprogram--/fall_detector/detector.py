"""
跌倒检测核心逻辑。

SlidingWindowBuffer: 滑动窗口缓存 CSI 幅度特征
FallDetector: 摄入样本 → 特征提取 → 窗口推理 → 告警判定
"""

from collections import deque
import logging
import time
from typing import Deque, List, Optional, Tuple

import numpy as np

from .config import SLIDING_WINDOW_SIZE, CONFIDENCE_THRESHOLD, ALERT_COOLDOWN_SEC

logger = logging.getLogger("fall_detector")


class SlidingWindowBuffer:
    """定长滑动窗口，自动丢弃最旧样本。"""

    def __init__(self, maxlen: int = SLIDING_WINDOW_SIZE):
        self._buffer: Deque[Tuple[float, float]] = deque(maxlen=maxlen)

    def push(self, timestamp: float, feature: float) -> None:
        self._buffer.append((timestamp, feature))

    def get_window(self) -> List[Tuple[float, float]]:
        return list(self._buffer)

    def is_full(self) -> bool:
        return len(self._buffer) >= self._buffer.maxlen

    def __len__(self) -> int:
        return len(self._buffer)


class FallDetector:
    """
    跌倒检测器。

    当前使用统计特征（方差）作为 stub 推理。
    后续可替换 _infer 方法接入 ML 模型（如 1D-CNN、Transformer）。
    """

    def __init__(
        self,
        window_size: int = SLIDING_WINDOW_SIZE,
        threshold: float = CONFIDENCE_THRESHOLD,
        cooldown_sec: float = ALERT_COOLDOWN_SEC,
    ):
        self._buffer = SlidingWindowBuffer(maxlen=window_size)
        self._threshold = threshold
        self._cooldown_sec = cooldown_sec
        self._last_alert_time: float = 0.0

    def ingest_sample(
        self,
        timestamp: float,
        amplitudes: np.ndarray,
    ) -> Optional[dict]:
        """
        摄入一帧 CSI 幅度数据。

        返回：
            None — 不足以判定或未检测到跌倒
            dict — 检测到跌倒时的告警数据
        """
        if amplitudes.size == 0:
            return None

        # 特征提取：当前使用平均幅度（后续可替换为更复杂的特征）
        feature = float(np.mean(amplitudes))
        self._buffer.push(timestamp, feature)

        if not self._buffer.is_full():
            return None

        window = self._buffer.get_window()
        confidence = self._infer(window)

        if confidence < self._threshold:
            return None

        if not self._check_cooldown():
            return None

        self._last_alert_time = time.time()
        duration = window[-1][0] - window[0][0]
        logger.info(
            "Fall detected! confidence=%.3f, duration=%.1fs",
            confidence, duration,
        )

        return {
            "type": "fall_alert",
            "device_id": "esp32s3_c_csi_2s_001",
            "alert_type": "fall_detected",
            "confidence": round(confidence, 4),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(timestamp)),
            "csi_snapshot_seq": 0,  # 后续从 CSI 帧中获取真实 seq
        }

    def _infer(self, window: List[Tuple[float, float]]) -> float:
        """
        Stub 推理函数。

        使用滑动窗口内特征方差作为异常指标。方差越大越可能异常。
        后续可用训练好的模型替代。
        """
        features = np.array([v for _, v in window], dtype=np.float32)
        mean = float(np.mean(features))
        variance = float(np.mean((features - mean) ** 2))

        # 将方差映射为 0~1 置信度（使用 tanh 平滑）
        # scale_factor 可调，控制敏感度
        scale_factor = 10.0
        confidence = float(np.tanh(variance * scale_factor))

        return confidence

    def _check_cooldown(self) -> bool:
        return (time.time() - self._last_alert_time) >= self._cooldown_sec
