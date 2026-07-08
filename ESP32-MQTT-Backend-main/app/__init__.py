from pathlib import Path
import threading

from flask import Flask
from sqlalchemy import inspect

from .config import configure_app
from .extensions import db, sock


_background_services_lock = threading.RLock()
_background_services_started = False


def _start_background_services(app: Flask, coordinator) -> None:
    global _background_services_started

    if app.config["TESTING"]:
        return
    if not (
        app.config["MQTT_AUTOSTART_DEVICES"]
        or app.config["OFFLINE_MONITOR_ENABLED"]
    ):
        return

    with _background_services_lock:
        if _background_services_started:
            app.logger.info("Background services already started; skipping")
            return

        if app.config["MQTT_AUTOSTART_DEVICES"]:
            coordinator.ensure_all_devices()
        coordinator.start_monitor()
        _background_services_started = True


def create_app(test_config: dict | None = None) -> Flask:
    """Create and configure the Flask application."""
    app = Flask(__name__, instance_relative_config=True)

    configure_app(app)

    if test_config:
        app.config.update(test_config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    sock.init_app(app)

    # Import models before create_all() so SQLAlchemy knows every table.
    from . import models  # noqa: F401
    from .api import api_bp
    from .cli import register_commands
    from .miniapp_api import miniapp_bp
    from .routes import site_bp
    from .schema import ensure_database_schema
    from .services.device_state_service import DeviceCoordinator
    from .services.websocket_service import register_websocket_routes

    app.register_blueprint(site_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(miniapp_bp)
    register_commands(app)
    register_websocket_routes(app)

    coordinator = DeviceCoordinator(app)
    app.extensions["device_coordinator"] = coordinator
    with app.app_context():
        ensure_database_schema()
        if "devices" in inspect(db.engine).get_table_names():
            _start_background_services(app, coordinator)

    return app
