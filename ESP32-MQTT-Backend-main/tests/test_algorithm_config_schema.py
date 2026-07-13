from sqlalchemy import inspect

from app.extensions import db
from app.models import Admin, Device, User
from app.schema import ensure_database_schema


def csrf_headers(token):
    return {"X-CSRF-Token": token}


def login_admin(client, app):
    with app.app_context():
        admin = Admin(username="admin")
        admin.set_password("safe-admin-password")
        db.session.add(admin)
        db.session.commit()
    client.get("/admin/login")
    with client.session_transaction() as session:
        csrf_token = session["csrf_token"]
    response = client.post(
        "/admin/login",
        data={
            "username": "admin",
            "password": "safe-admin-password",
            "csrf_token": csrf_token,
        },
    )
    assert response.status_code == 302
    with client.session_transaction() as session:
        return session["csrf_token"]


def test_admin_algorithm_config_validation_is_atomic(client, app):
    token = login_admin(client, app)
    with app.app_context():
        user = User(wx_openid="wx-config")
        device = Device(device_name="config-device", display_name="Config", owner=user)
        db.session.add_all([user, device])
        db.session.commit()
        device_id = device.id

    invalid = client.put(
        f"/api/devices/{device_id}/fall-algorithm-config",
        json={
            "step_size": 30,
            "buffer_size": 10,
            "fall_confidence_threshold": 0.8,
            "enable_sobel": True,
            "consecutive_required": 2,
            "confirmation_window": 4.0,
            "cooldown_seconds": 10.0,
            "max_time_interval": 1.5,
        },
        headers=csrf_headers(token),
    )
    assert invalid.status_code == 400
    with app.app_context():
        assert db.session.get(Device, device_id).buffer_size == 500

    valid = client.put(
        f"/api/devices/{device_id}/fall-algorithm-config",
        json={
            "step_size": 31,
            "buffer_size": 600,
            "fall_confidence_threshold": 0.7,
            "enable_sobel": False,
            "consecutive_required": 3,
            "confirmation_window": 5.0,
            "cooldown_seconds": 8.0,
            "max_time_interval": 1.2,
        },
        headers=csrf_headers(token),
    )
    assert valid.status_code == 200
    assert valid.get_json()["algorithm_config"] == {
        "step_size": 31,
        "buffer_size": 600,
        "fall_confidence_threshold": 0.7,
        "enable_sobel": False,
        "consecutive_required": 3,
        "confirmation_window": 5.0,
        "cooldown_seconds": 8.0,
        "max_time_interval": 1.2,
    }


def test_schema_adds_algorithm_columns_to_existing_sqlite_tables(app):
    with app.app_context():
        db.drop_all()
        db.session.execute(
            db.text(
                "CREATE TABLE devices ("
                "id INTEGER PRIMARY KEY, "
                "device_name VARCHAR(32) NOT NULL, "
                "owner_user_id INTEGER NOT NULL)"
            )
        )
        db.session.execute(
            db.text(
                "CREATE TABLE fall_events ("
                "id INTEGER PRIMARY KEY, "
                "device_id INTEGER NOT NULL, "
                "status VARCHAR(20) NOT NULL)"
            )
        )
        db.session.execute(
            db.text(
                "INSERT INTO devices (id, device_name, owner_user_id) "
                "VALUES (1, 'old-device', 1)"
            )
        )
        db.session.execute(
            db.text(
                "INSERT INTO fall_events (id, device_id, status) "
                "VALUES (1, 1, 'pending')"
            )
        )
        db.session.commit()

        changes = ensure_database_schema()
        repeat = ensure_database_schema()
        inspector = inspect(db.engine)
        device_columns = {column["name"] for column in inspector.get_columns("devices")}
        event_columns = {
            column["name"] for column in inspector.get_columns("fall_events")
        }
        row = db.session.execute(
            db.text(
                "SELECT step_size, buffer_size, enable_sobel FROM devices "
                "WHERE id = 1"
            )
        ).one()

        assert "devices.step_size" in changes
        assert "fall_events.alert_count" in changes
        assert repeat == []
        assert {"step_size", "buffer_size", "max_time_interval"} <= device_columns
        assert {"alert_count", "max_confidence", "algorithm_source"} <= event_columns
        assert tuple(row) == (30, 500, 1)


def test_database_has_no_algorithm_columns_on_users(app):
    with app.app_context():
        db.create_all()
        columns = {column["name"] for column in inspect(db.engine).get_columns("users")}

    assert "step_size" not in columns
    assert "buffer_size" not in columns
