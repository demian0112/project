from __future__ import annotations

from sqlalchemy import inspect

from .extensions import db


def ensure_database_schema() -> list[str]:
    """Create new tables and patch additive columns for existing SQLite DBs."""
    db.create_all()
    changes: list[str] = []
    changes.extend(_ensure_device_algorithm_columns())
    changes.extend(_ensure_fall_event_algorithm_columns())
    return changes


def _add_missing_columns(table: str, columns: dict[str, str]) -> list[str]:
    inspector = inspect(db.engine)
    if table not in inspector.get_table_names():
        return []

    existing = {
        column["name"]
        for column in inspector.get_columns(table)
    }
    applied: list[str] = []
    for name, definition in columns.items():
        if name in existing:
            continue
        db.session.execute(
            db.text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        )
        applied.append(f"{table}.{name}")

    if applied:
        db.session.commit()
    return applied


def _ensure_device_algorithm_columns() -> list[str]:
    return _add_missing_columns(
        "devices",
        {
            "step_size": "INTEGER NOT NULL DEFAULT 30",
            "buffer_size": "INTEGER NOT NULL DEFAULT 500",
            "fall_confidence_threshold": "FLOAT NOT NULL DEFAULT 0.8",
            "enable_sobel": "BOOLEAN NOT NULL DEFAULT 1",
            "consecutive_required": "INTEGER NOT NULL DEFAULT 2",
            "confirmation_window": "FLOAT NOT NULL DEFAULT 4.0",
            "cooldown_seconds": "FLOAT NOT NULL DEFAULT 10.0",
            "max_time_interval": "FLOAT NOT NULL DEFAULT 1.5",
        },
    )


def _ensure_fall_event_algorithm_columns() -> list[str]:
    return _add_missing_columns(
        "fall_events",
        {
            "wechat_notified": "BOOLEAN NOT NULL DEFAULT 0",
            "wechat_notified_at": "DATETIME",
            "wechat_notify_errcode": "INTEGER",
            "wechat_notify_errmsg": "VARCHAR(255)",
            "alert_count": "INTEGER NOT NULL DEFAULT 1",
            "last_detected_at": "DATETIME",
            "max_confidence": "FLOAT",
            "algorithm_source": "VARCHAR(40) NOT NULL DEFAULT 'docker'",
            "algorithm_class": "VARCHAR(64)",
            "algorithm_confidence": "FLOAT",
            "algorithm_timestamp": "DATETIME",
        },
    )
