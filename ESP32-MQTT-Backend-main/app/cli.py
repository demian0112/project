from __future__ import annotations

import click
from flask import Flask, current_app

from .extensions import db
from .models import Admin


def initialize_database(app: Flask) -> list[str]:
    """Create the current admin/users/devices/fall_events schema."""
    db.create_all()
    changes: list[str] = []

    username = app.config.get("INITIAL_ADMIN_USERNAME")
    password = app.config.get("INITIAL_ADMIN_PASSWORD")
    if username and password and db.session.scalar(db.select(Admin)) is None:
        admin = Admin(username=str(username).strip())
        admin.set_password(str(password))
        db.session.add(admin)
        db.session.commit()
        changes.append("initial administrator")

    return changes


def register_commands(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db_command() -> None:
        """Create the current database schema."""
        changes = initialize_database(app)
        database_path = db.engine.url.database
        click.echo(f"Database initialized: {database_path}")
        if changes:
            click.echo("Applied: " + ", ".join(changes))

    @app.cli.command("create-admin")
    @click.option("--username", prompt="Administrator username")
    @click.option(
        "--password",
        prompt="Administrator password",
        hide_input=True,
        confirmation_prompt=True,
    )
    def create_admin_command(username: str, password: str) -> None:
        """Create an administrator or reset an existing password."""
        initialize_database(app)
        username = username.strip()
        if not username:
            raise click.ClickException("Administrator username is required.")
        if len(password) < 8:
            raise click.ClickException(
                "Administrator password must contain at least 8 characters."
            )

        admin = db.session.scalar(
            db.select(Admin).where(Admin.username == username)
        )
        if admin is None:
            admin = Admin(username=username)
            db.session.add(admin)
            action = "created"
        else:
            action = "updated"

        admin.set_password(password)
        db.session.commit()
        click.echo(f"Administrator {username!r} {action}.")

    @app.cli.command("scan-offline")
    def scan_offline_command() -> None:
        """Run the device timeout check once (also runs periodically in app)."""
        count = current_app.extensions[
            "device_coordinator"
        ].scan_offline_devices()
        click.echo(f"Marked {count} device(s) offline.")
