from __future__ import annotations

from sqlalchemy import inspect

from .extensions import db


def ensure_database_schema() -> list[str]:
    """Create new tables and patch additive columns for existing SQLite DBs."""
    db.create_all()
    return _ensure_fall_event_notify_columns()


def _ensure_fall_event_notify_columns() -> list[str]:
    inspector = inspect(db.engine)
    if "fall_events" not in inspector.get_table_names():
        return []

    existing = {
        column["name"]
        for column in inspector.get_columns("fall_events")
    }
    columns = {
        "wechat_notified": "BOOLEAN NOT NULL DEFAULT 0",
        "wechat_notified_at": "DATETIME",
        "wechat_notify_errcode": "INTEGER",
        "wechat_notify_errmsg": "VARCHAR(255)",
    }
    applied: list[str] = []
    for name, definition in columns.items():
        if name in existing:
            continue
        db.session.execute(
            db.text(f"ALTER TABLE fall_events ADD COLUMN {name} {definition}")
        )
        applied.append(f"fall_events.{name}")

    if applied:
        db.session.commit()
    return applied
