from __future__ import annotations

from typing import Any


def predict_fall(
    device_name: str,
    session: str,
    csi_window: list[dict[str, Any]],
) -> int:
    """Algorithm integration point. The current safe default reports no fall."""
    del device_name, session, csi_window
    return 0

