from pathlib import Path

from flask import Flask
from sqlalchemy import inspect

from .config import configure_app
from .extensions import db, sock


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
        if "devices" in inspect(db.engine).get_table_names():
            coordinator.ensure_all_devices()
            coordinator.start_monitor()

    return app
