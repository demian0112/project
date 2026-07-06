import base64

from sqlalchemy import inspect

from app.extensions import db
from app.models import Admin, Device, FallEvent, User, utc_now
from app.services.csi_payload_service import BATCH_HEADER, FRAME_HEADER


def csi_payload(session, batch_no, seq0):
    sequences = (seq0, seq0 + 1)
    timestamps = (
        1_000_000 + (batch_no - 1) * 1_000_000,
        1_033_333 + (batch_no - 1) * 1_000_000,
    )
    parts = [
        BATCH_HEADER.pack(
            b"CSIB",
            0x01,
            len(sequences),
            30,
            0,
            batch_no,
            timestamps[0],
            timestamps[-1],
        )
    ]
    for sequence, timestamp in zip(sequences, timestamps):
        raw = b"\x01\x02\x03\x04"
        parts.extend(
            [
                FRAME_HEADER.pack(
                    sequence,
                    timestamp,
                    -45,
                    0,
                    len(raw),
                ),
                raw,
            ]
        )
    binary = b"".join(parts)
    return {
        "session": session,
        "batch": batch_no,
        "frames": len(sequences),
        "seq0": sequences[0],
        "seq1": sequences[-1],
        "ts0": timestamps[0],
        "ts1": timestamps[-1],
        "fmt": "csib64-v2",
        "bytes": len(binary),
        "data": base64.b64encode(binary).decode("ascii"),
        "ts": batch_no,
    }


def create_admin(app, username="admin", password="safe-admin-password"):
    with app.app_context():
        admin = Admin(username=username)
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()


def login_admin(
    client,
    app,
    username="admin",
    password="safe-admin-password",
):
    create_admin(app, username, password)
    client.get("/admin/login")
    with client.session_transaction() as session:
        csrf_token = session["csrf_token"]

    response = client.post(
        "/admin/login",
        data={
            "username": username,
            "password": password,
            "csrf_token": csrf_token,
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin")

    with client.session_transaction() as session:
        return session["csrf_token"]


def csrf_headers(token):
    return {"X-CSRF-Token": token}


def test_health(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_database_has_expected_tables_and_foreign_keys(app):
    with app.app_context():
        table_names = set(inspect(db.engine).get_table_names())
        foreign_keys_enabled = db.session.execute(
            db.text("PRAGMA foreign_keys")
        ).scalar()

    assert table_names == {"admin", "users", "devices", "fall_events"}
    assert foreign_keys_enabled == 1


def test_admin_login_protects_dashboard_and_api(client, app):
    dashboard_response = client.get("/admin")
    api_response = client.get("/api/users")

    assert dashboard_response.status_code == 302
    assert dashboard_response.headers["Location"].endswith("/admin/login")
    assert api_response.status_code == 401

    create_admin(app)
    client.get("/admin/login")
    with client.session_transaction() as session:
        csrf_token = session["csrf_token"]

    bad_login = client.post(
        "/admin/login",
        data={
            "username": "admin",
            "password": "wrong-password",
            "csrf_token": csrf_token,
        },
    )
    assert bad_login.status_code == 200
    assert "管理员账号或密码错误" in bad_login.get_data(as_text=True)

    good_login = client.post(
        "/admin/login",
        data={
            "username": "admin",
            "password": "safe-admin-password",
            "csrf_token": csrf_token,
        },
    )
    assert good_login.status_code == 302

    dashboard = client.get("/admin")
    assert dashboard.status_code == 200
    assert "设备管理中心" in dashboard.get_data(as_text=True)
    assert "跌倒事件" in dashboard.get_data(as_text=True)
    assert "网络质量" in dashboard.get_data(as_text=True)
    assert "/static/js/dashboard.js" in dashboard.get_data(as_text=True)


def test_admin_device_crud(client, app):
    token = login_admin(client, app)
    ensured_devices = []
    removed_devices = []
    app.extensions["device_coordinator"].mqtt.ensure_device = (
        lambda device_name: ensured_devices.append(device_name)
    )
    app.extensions["device_coordinator"].mqtt.remove_device = (
        lambda device_name: removed_devices.append(device_name)
    )

    with app.app_context():
        user = User(wx_openid="wx-device-owner", nickname="Alice")
        db.session.add(user)
        db.session.commit()
        user_id = user.id

    missing_csrf = client.post(
        "/api/devices",
        json={
            "device_uid": "esp32-no-csrf",
            "name": "No CSRF",
            "owner_id": user_id,
        },
    )
    assert missing_csrf.status_code == 403

    missing_owner = client.post(
        "/api/devices",
        json={
            "device_uid": "esp32-missing",
            "name": "Missing owner",
            "owner_id": 999,
        },
        headers=csrf_headers(token),
    )
    assert missing_owner.status_code == 404

    invalid_device_name = client.post(
        "/api/devices",
        json={
            "device_uid": "not a valid id",
            "name": "Bad ESP32",
            "owner_id": user_id,
        },
        headers=csrf_headers(token),
    )
    assert invalid_device_name.status_code == 400

    device_response = client.post(
        "/api/devices",
        json={
            "device_uid": "esp32-001",
            "name": "Living Room ESP32",
            "owner_id": user_id,
            "location": "Living room",
            "remark": "Main sensor",
        },
        headers=csrf_headers(token),
    )
    assert device_response.status_code == 201
    device_data = device_response.get_json()
    assert device_data["mqtt_topic"] == (
        "csi/v1/devices/esp32-001/up/csi"
    )
    assert device_data["owner_username"] == "Alice"
    assert ensured_devices == []

    duplicate_device = client.post(
        "/api/devices",
        json={
            "device_uid": "esp32-001",
            "name": "Duplicate",
            "owner_id": user_id,
        },
        headers=csrf_headers(token),
    )
    assert duplicate_device.status_code == 409

    immutable_uid = client.put(
        f"/api/devices/{device_data['id']}",
        json={"device_uid": "esp32-renamed"},
        headers=csrf_headers(token),
    )
    assert immutable_uid.status_code == 400

    updated_device = client.put(
        f"/api/devices/{device_data['id']}",
        json={
            "name": "Updated ESP32",
            "status": "disabled",
            "location": "Office",
        },
        headers=csrf_headers(token),
    )
    assert updated_device.status_code == 200
    assert updated_device.get_json()["device_uid"] == "esp32-001"
    assert updated_device.get_json()["status"] == "disabled"
    assert removed_devices == ["esp32-001"]

    deleted_device = client.delete(
        f"/api/devices/{device_data['id']}",
        headers=csrf_headers(token),
    )
    assert deleted_device.status_code == 200
    assert removed_devices == ["esp32-001", "esp32-001"]

    with app.app_context():
        assert db.session.get(Device, device_data["id"]) is None


def test_deleting_user_cascades_to_devices(client, app):
    token = login_admin(client, app)
    with app.app_context():
        user = User(wx_openid="wx-cascade", nickname="Bob")
        device = Device(
            device_name="esp32-bob",
            display_name="Bob ESP32",
            owner=user,
        )
        db.session.add_all([user, device])
        db.session.commit()
        user_id = user.id
        device_id = device.id

    response = client.delete(
        f"/api/users/{user_id}",
        headers=csrf_headers(token),
    )

    assert response.status_code == 200
    assert response.get_json()["deleted_devices"] == 1
    with app.app_context():
        assert db.session.get(User, user_id) is None
        assert db.session.get(Device, device_id) is None


def test_create_admin_cli(app):
    runner = app.test_cli_runner()

    result = runner.invoke(
        args=[
            "create-admin",
            "--username",
            "operator",
            "--password",
            "operator-password",
        ]
    )

    assert result.exit_code == 0
    assert "created" in result.output
    with app.app_context():
        admin = db.session.scalar(
            db.select(Admin).where(Admin.username == "operator")
        )
        assert admin is not None
        assert admin.check_password("operator-password")


def test_admin_can_manage_wechat_profile_and_fall_events(client, app):
    token = login_admin(client, app)

    with app.app_context():
        user = User(
            wx_openid="wx-admin-view",
            nickname="原昵称",
            phone="13800000000",
        )
        device = Device(
            device_name="admin-fall-device",
            display_name="客厅检测器",
            owner=user,
            location="客厅",
        )
        event = FallEvent(
            user=user,
            device=device,
            device_name=device.device_name,
            session="sess-admin-view",
            result=1,
            network_quality="good",
            occurred_at=utc_now(),
            status="pending",
            notified=True,
        )
        db.session.add_all([user, device, event])
        db.session.commit()
        user_id = user.id
        event_id = event.id

    updated_user = client.put(
        f"/api/users/{user_id}",
        json={
            "nickname": "新昵称",
            "phone": "13900000000",
            "role": "user",
            "status": "disabled",
        },
        headers=csrf_headers(token),
    )
    assert updated_user.status_code == 200
    assert updated_user.get_json()["nickname"] == "新昵称"
    assert updated_user.get_json()["phone"] == "13900000000"
    assert updated_user.get_json()["status"] == "disabled"
    assert "wx_openid" not in updated_user.get_json()

    listed = client.get("/api/fall-events")
    assert listed.status_code == 200
    assert listed.get_json()[0]["id"] == event_id
    assert listed.get_json()[0]["owner_name"] == "新昵称"
    assert listed.get_json()[0]["device_name"] == "admin-fall-device"

    missing_csrf = client.patch(
        f"/api/fall-events/{event_id}",
        json={"status": "ignored"},
    )
    assert missing_csrf.status_code == 403

    handled = client.patch(
        f"/api/fall-events/{event_id}",
        json={"status": "ignored"},
        headers=csrf_headers(token),
    )
    assert handled.status_code == 200
    assert handled.get_json()["status"] == "ignored"
    assert handled.get_json()["handled_at"] is not None


def login_miniapp(client, app, openid="wx-openid-001"):
    app.config["WECHAT_CODE_EXCHANGE"] = lambda code: {
        "openid": openid,
        "unionid": f"union-{openid}",
        "session_key": f"secret-for-{code}",
    }
    response = client.post(
        "/api/v1/auth/wechat-login",
        json={"code": "one-time-code"},
    )
    assert response.status_code == 200
    return response.get_json()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_wechat_login_token_and_profile(client, app):
    missing_code = client.post(
        "/api/v1/auth/wechat-login",
        json={},
    )
    assert missing_code.status_code == 400
    assert missing_code.get_json()["error"] == "INVALID_WECHAT_CODE"

    login_data = login_miniapp(client, app)
    assert login_data["user"]["is_new_user"] is True
    assert "session_key" not in str(login_data)

    token = login_data["access_token"]
    me = client.get("/api/v1/me", headers=bearer(token))
    assert me.status_code == 200
    assert me.get_json()["nickname"] is None
    assert "wx_openid" not in me.get_json()

    updated = client.patch(
        "/api/v1/me/profile",
        json={
            "nickname": "安心用户",
            "avatar_url": "https://example.test/avatar.png",
        },
        headers=bearer(token),
    )
    assert updated.status_code == 200
    assert updated.get_json()["nickname"] == "安心用户"

    missing_phone_code = client.post(
        "/api/v1/me/phone",
        json={},
        headers=bearer(token),
    )
    assert missing_phone_code.status_code == 400
    assert missing_phone_code.get_json()["error"] == "INVALID_PHONE_CODE"

    app.config["WECHAT_PHONE_NUMBER_EXCHANGE"] = lambda code: {
        "errcode": 0,
        "phone_info": {
            "phoneNumber": "+8613800000000",
            "purePhoneNumber": "13800000000",
        },
    }
    updated_phone = client.post(
        "/api/v1/me/phone",
        json={"code": "phone-code-001"},
        headers=bearer(token),
    )
    assert updated_phone.status_code == 200
    assert updated_phone.get_json()["phone"] == "13800000000"

    second_login = login_miniapp(client, app)
    assert second_login["user"]["is_new_user"] is False
    with app.app_context():
        user = db.session.scalar(
            db.select(User).where(User.wx_openid == "wx-openid-001")
        )
        assert user.wx_session_key_enc is None
        assert user.wx_unionid == "union-wx-openid-001"
        assert user.phone == "13800000000"


def test_miniapp_silent_login_does_not_create_new_user(client, app):
    app.config["WECHAT_CODE_EXCHANGE"] = lambda code: {
        "openid": "wx-silent-new",
        "session_key": f"secret-for-{code}",
    }
    missing = client.post(
        "/api/v1/auth/wechat-login",
        json={"code": "silent-code", "create_if_missing": False},
    )
    assert missing.status_code == 404
    assert missing.get_json()["error"] == "USER_NOT_REGISTERED"

    with app.app_context():
        assert db.session.scalar(db.select(User)) is None

    created = client.post(
        "/api/v1/auth/wechat-login",
        json={"code": "normal-code"},
    )
    assert created.status_code == 200

    restored = client.post(
        "/api/v1/auth/wechat-login",
        json={"code": "silent-code", "create_if_missing": False},
    )
    assert restored.status_code == 200
    assert restored.get_json()["user"]["is_new_user"] is False


def test_miniapp_device_ownership_and_control(client, app):
    login_data = login_miniapp(client, app)
    token = login_data["access_token"]
    user_id = login_data["user"]["id"]
    published = []
    ensured_devices = []
    app.extensions["device_coordinator"].mqtt.ensure_device = (
        lambda device_name: ensured_devices.append(device_name)
    )
    app.config["MQTT_CONTROL_PUBLISHER"] = (
        lambda **message: published.append(message)
    )

    with app.app_context():
        device = Device(
            device_name="csi-gw-001",
            display_name="客厅设备",
            owner_user_id=user_id,
            state="online",
            last_status_at=utc_now(),
        )
        another_user = User(wx_openid="wx-other")
        another_device = Device(
            device_name="csi-gw-other",
            display_name="其他设备",
            owner=another_user,
        )
        db.session.add_all([device, another_user, another_device])
        db.session.commit()

    listing = client.get("/api/v1/devices", headers=bearer(token))
    assert listing.status_code == 200
    assert [item["device_name"] for item in listing.get_json()["items"]] == [
        "csi-gw-001"
    ]
    assert ensured_devices == ["csi-gw-001"]

    forbidden = client.get(
        "/api/v1/devices/csi-gw-other",
        headers=bearer(token),
    )
    assert forbidden.status_code == 403
    assert forbidden.get_json()["error"] == "DEVICE_NOT_OWNED"

    headers = {
        **bearer(token),
        "Idempotency-Key": "control-start-001",
    }
    started = client.post(
        "/api/v1/devices/csi-gw-001/control",
        json={"action": "start"},
        headers=headers,
    )
    repeated = client.post(
        "/api/v1/devices/csi-gw-001/control",
        json={"action": "start"},
        headers=headers,
    )
    assert started.status_code == repeated.status_code == 202
    assert started.get_json() == repeated.get_json()
    assert len(published) == 1
    session = started.get_json()["session"]

    with app.app_context():
        coordinator = app.extensions["device_coordinator"]
        coordinator.handle_mqtt_payload(
            "csi-gw-001",
            "status",
            {
                "state": "uploading",
                "session": session,
                "uart": True,
                "upload": True,
                "ts": 241,
            },
        )
        device = db.session.scalar(
            db.select(Device).where(
                Device.device_name == "csi-gw-001"
            )
        )
        assert device.runtime_state == "uploading"
        assert device.detection_state == "running"
        assert device.current_session == session

        coordinator.handle_mqtt_payload(
            "csi-gw-001",
            "ack",
            {
                "cmd": "control",
                "action": "start",
                "ok": True,
                "state": "uploading",
                "err": 0,
                "msg": "",
                "ts": 240,
            },
        )

    stopped = client.post(
        "/api/v1/devices/csi-gw-001/control",
        json={"action": "stop"},
        headers={
            **bearer(token),
            "Idempotency-Key": "control-stop-001",
        },
    )
    assert stopped.status_code == 202
    assert stopped.get_json()["session"] == session
    assert [item["action"] for item in published] == ["start", "stop"]

    with app.app_context():
        coordinator.handle_mqtt_payload(
            "csi-gw-001",
            "status",
            {
                "state": "idle",
                "session": "",
                "uart": True,
                "upload": False,
                "ts": 261,
            },
        )
        device = db.session.scalar(
            db.select(Device).where(
                Device.device_name == "csi-gw-001"
            )
        )
        assert device.runtime_state == "idle"
        assert device.detection_state == "idle"
        assert device.current_session is None

        coordinator.handle_mqtt_payload(
            "csi-gw-001",
            "ack",
            {
                "cmd": "control",
                "action": "stop",
                "ok": True,
                "state": "idle",
                "err": 0,
                "msg": "",
                "ts": 260,
            },
        )
        device = db.session.scalar(
            db.select(Device).where(
                Device.device_name == "csi-gw-001"
            )
        )
        assert device.detection_state == "idle"
        assert device.current_session is None


def test_hardware_status_fault_auto_stop_and_offline_contract(app):
    published = []
    app.config["MQTT_CONTROL_PUBLISHER"] = (
        lambda **message: published.append(message)
    )

    with app.app_context():
        user = User(wx_openid="wx-hardware-contract")
        device = Device(
            device_name="contract-device",
            owner=user,
            state="offline",
        )
        db.session.add_all([user, device])
        db.session.commit()
        coordinator = app.extensions["device_coordinator"]

        coordinator.handle_mqtt_payload(
            device.device_name,
            "online",
            {"status": "online", "ts": 10},
        )
        coordinator.handle_mqtt_payload(
            device.device_name,
            "status",
            {
                "state": "uploading",
                "session": "sess-hardware-001",
                "uart": True,
                "upload": True,
                "ts": 11,
            },
        )
        db.session.refresh(device)
        assert device.state == "online"
        assert device.runtime_state == "uploading"
        assert device.detection_state == "running"
        assert device.current_session == "sess-hardware-001"

        coordinator.handle_mqtt_payload(
            device.device_name,
            "fault",
            {
                "code": "UART_TIMEOUT",
                "msg": "no csi frame from b board",
                "state": "fault",
                "ts": 12,
            },
        )
        db.session.refresh(device)
        assert device.state == "error"
        assert device.runtime_state == "fault"
        assert device.detection_state == "stopping"
        assert device.fault_message == "no csi frame from b board"
        assert published[-1]["device_name"] == "contract-device"
        assert published[-1]["action"] == "stop"
        assert published[-1]["session"] == "sess-hardware-001"
        assert published[-1]["command_id"].startswith("fault-stop-")

        coordinator.handle_mqtt_payload(
            device.device_name,
            "ack",
            {
                "cmd": "control",
                "action": "stop",
                "ok": True,
                "state": "idle",
                "err": 0,
                "msg": "",
                "ts": 13,
            },
        )
        db.session.refresh(device)
        assert device.state == "error"
        assert device.runtime_state == "idle"
        assert device.detection_state == "idle"
        assert device.current_session is None

        coordinator.handle_mqtt_payload(
            device.device_name,
            "online",
            {"status": "offline", "reason": "lwt"},
        )
        db.session.refresh(device)
        assert device.state == "offline"


def test_mqtt_state_csi_fall_event_and_offline_scan(client, app):
    login_data = login_miniapp(client, app)
    token = login_data["access_token"]
    user_id = login_data["user"]["id"]
    app.config["CSI_WINDOW_SIZE"] = 2
    app.config["FALL_PREDICTOR"] = (
        lambda device_name, session, window: 1
    )

    with app.app_context():
        device = Device(
            device_name="fall-room-01",
            display_name="卧室设备",
            owner_user_id=user_id,
        )
        db.session.add(device)
        db.session.commit()

        coordinator = app.extensions["device_coordinator"]
        coordinator.handle_mqtt_payload(
            "fall-room-01",
            "online",
            {
                "status": "online",
                "ip": "192.168.1.122",
                "fw": "csi-softap-mqtt-1.0.0",
                "ts": 240,
            },
        )
        coordinator.handle_mqtt_payload(
            "fall-room-01",
            "fault",
            {"code": "SENSOR", "msg": "传感器异常"},
        )
        db.session.refresh(device)
        assert device.state == "error"

        coordinator.handle_mqtt_payload(
            "fall-room-01",
            "online",
            {"status": "online"},
        )
        db.session.refresh(device)
        assert device.state == "online"
        assert device.fault_code is None

        device.current_session = "sess-test-001"
        device.detection_state = "running"
        db.session.commit()
        coordinator.handle_mqtt_payload(
            "fall-room-01",
            "csi",
            csi_payload("sess-test-001", 1, 1),
        )
        coordinator.handle_mqtt_payload(
            "fall-room-01",
            "csi",
            csi_payload("sess-test-001", 2, 3),
        )

        event = db.session.scalar(db.select(FallEvent))
        assert event is not None
        assert event.status == "pending"
        assert event.notified is True
        assert event.network_quality == "good"

    events = client.get(
        "/api/v1/fall-events?limit=20",
        headers=bearer(token),
    )
    assert events.status_code == 200
    event_id = events.get_json()["items"][0]["id"]

    handled = client.patch(
        f"/api/v1/fall-events/{event_id}",
        json={"status": "confirmed"},
        headers=bearer(token),
    )
    assert handled.status_code == 200
    assert handled.get_json()["item"]["status"] == "confirmed"

    with app.app_context():
        device = db.session.scalar(
            db.select(Device).where(
                Device.device_name == "fall-room-01"
            )
        )
        device.last_seen_at = utc_now().replace(year=2020)
        device.state = "online"
        db.session.commit()
        count = app.extensions[
            "device_coordinator"
        ].scan_offline_devices()
        db.session.refresh(device)
        assert count == 1
        assert device.state == "offline"
        assert device.network_quality == "unknown"
