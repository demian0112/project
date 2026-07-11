from __future__ import annotations

from typing import Any


ALGORITHM_CONFIG_FIELDS = (
    "step_size",
    "buffer_size",
    "fall_confidence_threshold",
    "enable_sobel",
    "consecutive_required",
    "confirmation_window",
    "cooldown_seconds",
    "max_time_interval",
)

ALGORITHM_CONFIG_DEFAULTS = {
    "step_size": 30,
    "buffer_size": 500,
    "fall_confidence_threshold": 0.8,
    "enable_sobel": True,
    "consecutive_required": 2,
    "confirmation_window": 4.0,
    "cooldown_seconds": 10.0,
    "max_time_interval": 1.5,
}

_RANGES = {
    "step_size": (1, 10_000, int),
    "buffer_size": (94, 100_000, int),
    "fall_confidence_threshold": (0.0, 1.0, float),
    "consecutive_required": (1, 1_000, int),
    "confirmation_window": (0.001, 3_600.0, float),
    "cooldown_seconds": (0.0, 3_600.0, float),
    "max_time_interval": (0.001, 60.0, float),
}


class AlgorithmConfigError(ValueError):
    """Raised when an administrator submits invalid algorithm settings."""


def device_algorithm_config(device: Any) -> dict[str, Any]:
    """Return the per-device Docker algorithm config sent to POST /config."""
    return {
        field: getattr(device, field, ALGORITHM_CONFIG_DEFAULTS[field])
        for field in ALGORITHM_CONFIG_FIELDS
    }


def validate_algorithm_config(
    data: dict[str, Any],
    *,
    current: Any | None = None,
) -> dict[str, Any]:
    """Validate and normalize the complete editable algorithm configuration."""
    normalized: dict[str, Any] = {}
    source = data or {}
    for field in ALGORITHM_CONFIG_FIELDS:
        if field in source:
            value = source[field]
        elif current is not None:
            value = getattr(current, field)
        else:
            value = ALGORITHM_CONFIG_DEFAULTS[field]

        if field == "enable_sobel":
            normalized[field] = _parse_bool(value, field)
            continue

        lower, upper, caster = _RANGES[field]
        normalized[field] = _parse_number(
            value,
            field,
            minimum=lower,
            maximum=upper,
            caster=caster,
        )

    return normalized


def apply_algorithm_config(device: Any, values: dict[str, Any]) -> None:
    for field in ALGORITHM_CONFIG_FIELDS:
        setattr(device, field, values[field])


def _parse_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise AlgorithmConfigError(f"{field} must be a boolean")


def _parse_number(
    value: Any,
    field: str,
    *,
    minimum: float,
    maximum: float,
    caster: type[int] | type[float],
) -> int | float:
    if isinstance(value, bool):
        raise AlgorithmConfigError(f"{field} must be a number")
    try:
        parsed = caster(value)
    except (TypeError, ValueError) as exc:
        raise AlgorithmConfigError(f"{field} must be a number") from exc
    if parsed < minimum or parsed > maximum:
        raise AlgorithmConfigError(
            f"{field} must be between {minimum:g} and {maximum:g}"
        )
    return parsed
