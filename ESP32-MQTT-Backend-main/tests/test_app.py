import base64
from datetime import timedelta

import pytest
from sqlalchemy import func, inspect

from app.extensions import db
from app.models import (
    Admin,
    Device,
    FallEvent,
    User,
    WxNotifyLog,
    WxSubscription,
    utc_now,
)
from app.services.fall_alert_service import FallAlertService
from app.services.fall_algorithm_client import AlgorithmAlert
from app.services.csi_payload_service import BATCH_HEADER, FRAME_HEADER


def csi_payload(
    session,
    batch_no,
    seq0,
    *,
    frame_count=2,
    batch_interval_us=1_000_000,
):
    sequences = tuple(seq0 + index for index in range(frame_count))
    batch_start = 1_000_000 + (batch_no - 1) * batch_interval_us
    timestamps = (
        batch_start,
        *(
            batch_start + index * 33_333
            for index in range(1, frame_count)
        ),
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

    assert table_names == {
        "admin",
        "users",
        "devices",
        "fall_events",
        "wx_subscriptions",
        "wx_notify_logs",
    }
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


def test_miniapp_records_wechat_subscription(client, app):
    app.config["WECHAT_FALL_ALERT_TEMPLATE_ID"] = "tpl-fall-alert"
    login_data = login_miniapp(client, app)
    token = login_data["access_token"]

    accepted = client.post(
        "/api/v1/wechat/subscriptions",
        json={
            "scene": "fall_alert",
            "template_id": "tpl-fall-alert",
            "status": "accept",
        },
        headers=bearer(token),
    )
    assert accepted.status_code == 200
    assert accepted.get_json()["remaining_count"] == 1

    rejected = client.post(
        "/api/v1/wechat/subscriptions",
        json={
            "scene": "fall_alert",
            "template_id": "tpl-fall-alert",
            "status": "reject",
        },
        headers=bearer(token),
    )
    assert rejected.status_code == 200
    assert rejected.get_json()["status"] == "reject"
    assert rejected.get_json()["remaining_count"] == 0

    status = client.get(
        "/api/v1/wechat/subscriptions",
        headers=bearer(token),
    )
    assert status.status_code == 200
    assert status.get_json()["remaining_count"] == 0


def test_device_fault_notice_links_to_fault_device_detail(app):
    from urllib.parse import parse_qs, urlsplit

    from app.services.wechat_notify_service import send_device_fault_notice

    app.config["WECHAT_NOTIFY_ENABLED"] = True
    app.config["WECHAT_DEVICE_FAULT_TEMPLATE_ID"] = "tpl-device-fault"
    app.config["WECHAT_DEVICE_FAULT_PAGE"] = (
        "pages/device-detail/index?source=wechat&deviceName=old-device"
    )
    sent_messages = []
    app.config["WECHAT_SUBSCRIBE_SENDER"] = (
        lambda **message: sent_messages.append(message)
        or {"errcode": 0, "errmsg": "ok"}
    )

    with app.app_context():
        user = User(wx_openid="wx-device-fault")
        device = Device(
            device_name="fault-notice-device",
            display_name="Fault Notice Device",
            owner=user,
            location="Living Room",
            state="error",
            runtime_state="fault",
            fault_code="NO_CSI_FRAME",
            fault_message="no csi",
        )
        db.session.add_all([user, device])
        db.session.flush()
        db.session.add(
            WxSubscription(
                user_id=user.id,
                scene="device_fault",
                template_id="tpl-device-fault",
                status="accept",
                remaining_count=1,
                last_subscribed_at=utc_now(),
            )
        )
        db.session.commit()

        result = send_device_fault_notice(
            user,
            device,
            code="NO_CSI_FRAME",
            message="no csi",
        )

    assert result["sent"] is True
    assert len(sent_messages) == 1
    page = sent_messages[0]["page"]
    parsed = urlsplit(page)
    query = parse_qs(parsed.query)
    assert parsed.path == "pages/device-detail/index"
    assert query["source"] == ["wechat"]
    assert query["deviceName"] == ["fault-notice-device"]
    assert page.count("deviceName=") == 1


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


def test_admin_can_simulate_fall_and_send_wechat_notify(client, app):
    app.config["WECHAT_NOTIFY_ENABLED"] = True
    app.config["WECHAT_FALL_ALERT_TEMPLATE_ID"] = "tpl-fall-alert"
    sent_messages = []
    app.config["WECHAT_SUBSCRIBE_SENDER"] = (
        lambda **message: sent_messages.append(message)
        or {"errcode": 0, "errmsg": "ok"}
    )
    csrf_token = login_admin(client, app)

    with app.app_context():
        user = User(wx_openid="wx-simulated-fall", nickname="Sim User")
        device = Device(
            device_name="sim-fall-device",
            display_name="Sim Fall Device",
            owner=user,
            location="Bedroom",
            state="online",
            runtime_state="uploading",
            detection_state="running",
            current_session="real-session-kept",
            network_quality="good",
        )
        db.session.add_all([user, device])
        db.session.flush()
        subscription = WxSubscription(
            user_id=user.id,
            scene="fall_alert",
            template_id="tpl-fall-alert",
            status="accept",
            remaining_count=0,
            last_subscribed_at=utc_now(),
        )
        db.session.add(subscription)
        db.session.commit()
        device_id = device.id

    response = client.post(
        f"/api/devices/{device_id}/simulate-fall",
        json={"send_wechat": True},
        headers=csrf_headers(csrf_token),
    )

    assert response.status_code == 201
    body = response.get_json()
    assert body["ok"] is True
    assert body["fall_event"]["device_name"] == "sim-fall-device"
    assert body["wechat"]["sent"] is True
    assert body["wechat"]["remaining_count"] == 1
    assert sent_messages[0]["openid"] == "wx-simulated-fall"
    assert sent_messages[0]["page"].startswith("pages/fall-alert/index?id=")
    assert set(sent_messages[0]["data"]) == {
        "thing1",
        "time2",
        "thing3",
        "thing5",
    }

    with app.app_context():
        event = db.session.scalar(db.select(FallEvent))
        assert event is not None
        assert event.status == "pending"
        assert event.session.startswith("admin-simulated-")
        assert event.wechat_notified is True
        device = db.session.get(Device, device_id)
        assert device.detection_state == "running"
        assert device.runtime_state == "uploading"
        assert device.current_session == "real-session-kept"
        subscription = db.session.scalar(db.select(WxSubscription))
        assert subscription.remaining_count == 1
        log = db.session.scalar(db.select(WxNotifyLog))
        assert log.success is True
        assert log.openid_masked == "wx-simul***"

    second_response = client.post(
        f"/api/devices/{device_id}/simulate-fall",
        json={"send_wechat": True},
        headers=csrf_headers(csrf_token),
    )

    assert second_response.status_code == 201
    assert second_response.get_json()["wechat"]["sent"] is True
    assert second_response.get_json()["wechat"]["remaining_count"] == 1
    assert len(sent_messages) == 2

    with app.app_context():
        assert db.session.scalar(db.select(WxSubscription)).remaining_count == 1
        assert db.session.scalar(db.select(func.count(FallEvent.id))) == 2
        assert db.session.scalar(db.select(func.count(WxNotifyLog.id))) == 2


def test_admin_simulated_fall_survives_missing_wechat_subscription(client, app):
    app.config["WECHAT_NOTIFY_ENABLED"] = True
    app.config["WECHAT_FALL_ALERT_TEMPLATE_ID"] = "tpl-fall-alert"
    sent_messages = []
    app.config["WECHAT_SUBSCRIBE_SENDER"] = (
        lambda **message: sent_messages.append(message)
        or {"errcode": 0, "errmsg": "ok"}
    )
    csrf_token = login_admin(client, app)

    with app.app_context():
        user = User(wx_openid="wx-no-subscription", nickname="No Sub")
        device = Device(
            device_name="no-sub-device",
            display_name="No Sub Device",
            owner=user,
            network_quality="unknown",
        )
        db.session.add_all([user, device])
        db.session.commit()
        device_id = device.id

    response = client.post(
        f"/api/devices/{device_id}/simulate-fall",
        json={"send_wechat": True},
        headers=csrf_headers(csrf_token),
    )

    assert response.status_code == 201
    body = response.get_json()
    assert body["ok"] is True
    assert body["wechat"]["sent"] is False
    assert body["wechat"]["reason"] == "subscription_not_found"
    assert sent_messages == []
    with app.app_context():
        assert db.session.scalar(db.select(FallEvent)) is not None
        log = db.session.scalar(db.select(WxNotifyLog))
        assert log.success is False
        assert log.errcode == 43101


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


def test_miniapp_reset_fault_allows_error_device_and_ack_ok_clears(client, app):
    login_data = login_miniapp(client, app, openid="wx-reset-ok")
    token = login_data["access_token"]
    user_id = login_data["user"]["id"]
    published = []
    app.config["MQTT_CONTROL_PUBLISHER"] = (
        lambda **message: published.append(message)
    )

    with app.app_context():
        device = Device(
            device_name="reset-ok-device",
            display_name="Reset OK Device",
            owner_user_id=user_id,
            state="error",
            runtime_state="fault",
            detection_state="idle",
            current_session="sess-reset-ok",
            network_quality="poor",
            fault_code="NO_CSI_FRAME_TIMEOUT",
            fault_message="no csi frame",
        )
        db.session.add(device)
        db.session.commit()

    response = client.post(
        "/api/v1/devices/reset-ok-device/reset-fault",
        headers={
            **bearer(token),
            "Idempotency-Key": "reset-ok-001",
        },
    )

    assert response.status_code == 202
    assert response.get_json()["action"] == "reset"
    assert published == [
        {
            "device_name": "reset-ok-device",
            "action": "reset",
            "session": "sess-reset-ok",
            "command_id": "reset-ok-001",
            "reason": "user_fault_confirm",
            "source": "user",
        }
    ]

    with app.app_context():
        coordinator = app.extensions["device_coordinator"]
        coordinator.handle_mqtt_payload(
            "reset-ok-device",
            "ack",
            {
                "cmd": "control",
                "action": "reset",
                "ok": True,
                "state": "idle",
                "session": "sess-reset-ok",
                "err": 0,
                "msg": "",
            },
        )
        device = db.session.scalar(
            db.select(Device).where(
                Device.device_name == "reset-ok-device"
            )
        )
        assert device.state == "online"
        assert device.runtime_state == "idle"
        assert device.detection_state == "idle"
        assert device.current_session is None
        assert device.network_quality == "unknown"
        assert device.fault_code is None
        assert device.fault_message is None


def test_reset_ack_false_preserves_fault_and_blocks_plain_idle_clear(client, app):
    login_data = login_miniapp(client, app, openid="wx-reset-fail")
    token = login_data["access_token"]
    user_id = login_data["user"]["id"]
    published = []
    app.config["MQTT_CONTROL_PUBLISHER"] = (
        lambda **message: published.append(message)
    )

    with app.app_context():
        device = Device(
            device_name="reset-fail-device",
            owner_user_id=user_id,
            state="error",
            runtime_state="fault",
            detection_state="idle",
            current_session="sess-reset-fail",
            fault_code="UART_TIMEOUT",
            fault_message="uart timeout",
        )
        db.session.add(device)
        db.session.commit()

    response = client.post(
        "/api/v1/devices/reset-fail-device/reset-fault",
        headers={
            **bearer(token),
            "Idempotency-Key": "reset-fail-001",
        },
    )
    assert response.status_code == 202
    assert published[-1]["action"] == "reset"

    with app.app_context():
        coordinator = app.extensions["device_coordinator"]
        coordinator.handle_mqtt_payload(
            "reset-fail-device",
            "ack",
            {
                "cmd": "control",
                "action": "reset",
                "ok": False,
                "state": "fault",
                "session": "sess-reset-fail",
                "err": 1,
                "msg": "reset failed",
            },
        )
        coordinator.handle_mqtt_payload(
            "reset-fail-device",
            "status",
            {
                "state": "idle",
                "session": "sess-reset-fail",
            },
        )
        device = db.session.scalar(
            db.select(Device).where(
                Device.device_name == "reset-fail-device"
            )
        )
        assert device.state == "error"
        assert device.runtime_state == "fault"
        assert device.fault_code == "UART_TIMEOUT"
        assert device.fault_message == "uart timeout"


def test_pending_reset_idle_status_clears_fault(client, app):
    login_data = login_miniapp(client, app, openid="wx-reset-idle")
    token = login_data["access_token"]
    user_id = login_data["user"]["id"]
    app.config["MQTT_CONTROL_PUBLISHER"] = lambda **message: None

    with app.app_context():
        device = Device(
            device_name="reset-idle-device",
            owner_user_id=user_id,
            state="error",
            runtime_state="fault",
            detection_state="idle",
            current_session="sess-reset-idle",
            fault_code="NO_CSI_FRAME",
            fault_message="no csi",
        )
        db.session.add(device)
        db.session.commit()

    response = client.post(
        "/api/v1/devices/reset-idle-device/reset-fault",
        headers={
            **bearer(token),
            "Idempotency-Key": "reset-idle-001",
        },
    )
    assert response.status_code == 202

    with app.app_context():
        coordinator = app.extensions["device_coordinator"]
        coordinator.handle_mqtt_payload(
            "reset-idle-device",
            "status",
            {
                "state": "idle",
                "session": "sess-reset-idle",
            },
        )
        device = db.session.scalar(
            db.select(Device).where(
                Device.device_name == "reset-idle-device"
            )
        )
        assert device.state == "online"
        assert device.runtime_state == "idle"
        assert device.detection_state == "idle"
        assert device.fault_code is None
        assert device.fault_message is None


def test_start_can_be_confirmed_by_first_csi_without_fresh_status(client, app):
    login_data = login_miniapp(client, app, openid="wx-c-board-start")
    token = login_data["access_token"]
    user_id = login_data["user"]["id"]
    published = []
    app.config["MQTT_CONTROL_PUBLISHER"] = (
        lambda **message: published.append(message)
    )

    with app.app_context():
        device = Device(
            device_name="c-board-start",
            owner_user_id=user_id,
            state="online",
            last_seen_at=utc_now() - timedelta(minutes=2),
            last_status_at=utc_now() - timedelta(minutes=2),
        )
        db.session.add(device)
        db.session.commit()

    response = client.post(
        "/api/v1/devices/c-board-start/control",
        json={"action": "start"},
        headers={**bearer(token), "Idempotency-Key": "c-board-start"},
    )
    assert response.status_code == 202
    session = response.get_json()["session"]
    assert published[0]["reason"] == "user_start"

    with app.app_context():
        coordinator = app.extensions["device_coordinator"]
        coordinator.handle_mqtt_payload(
            "c-board-start",
            "csi",
            csi_payload(
                session,
                1,
                1,
                frame_count=45,
                batch_interval_us=1_500_000,
            ),
        )
        device = db.session.scalar(
            db.select(Device).where(Device.device_name == "c-board-start")
        )
        assert device.state == "online"
        assert device.runtime_state == "uploading"
        assert device.detection_state == "running"
        assert device.current_session == session
        assert device.last_csi_at is not None


def test_running_csi_gap_degrades_quality_before_hard_fault(app):
    published = []
    app.config["MQTT_CONTROL_PUBLISHER"] = (
        lambda **message: published.append(message)
    )

    with app.app_context():
        user = User(wx_openid="wx-csi-gap")
        device = Device(
            device_name="csi-gap-device",
            owner=user,
            state="online",
            runtime_state="uploading",
            detection_state="running",
            current_session="sess-gap",
            network_quality="good",
            last_seen_at=utc_now() - timedelta(seconds=12),
            last_status_at=utc_now() - timedelta(minutes=5),
            last_csi_at=utc_now() - timedelta(seconds=12),
        )
        db.session.add_all([user, device])
        db.session.commit()

        count = app.extensions["device_coordinator"].scan_offline_devices()
        db.session.refresh(device)
        assert count == 0
        assert device.state == "online"
        assert device.runtime_state == "uploading"
        assert device.detection_state == "running"
        assert device.current_session == "sess-gap"
        assert device.network_quality == "poor"

        device.last_seen_at = utc_now() - timedelta(seconds=35)
        device.last_csi_at = utc_now() - timedelta(seconds=35)
        db.session.commit()
        count = app.extensions["device_coordinator"].scan_offline_devices()
        db.session.refresh(device)
        assert count == 0
        assert device.state == "error"
        assert device.runtime_state == "fault"
        assert device.detection_state == "stopping"
        assert device.current_session == "sess-gap"
        assert device.fault_code == "NO_CSI_FRAME_TIMEOUT"
        assert published[-1]["action"] == "stop"


def test_offline_scan_rolls_back_on_failure(monkeypatch, app):
    with app.app_context():
        coordinator = app.extensions["device_coordinator"]
        calls = []
        original_rollback = db.session.rollback

        def fail_scan():
            raise RuntimeError("scan failed")

        def rollback():
            calls.append("rollback")
            original_rollback()

        monkeypatch.setattr(coordinator, "_scan_offline_devices", fail_scan)
        monkeypatch.setattr(db.session, "rollback", rollback)

        with pytest.raises(RuntimeError, match="scan failed"):
            coordinator.scan_offline_devices()

        assert calls == ["rollback"]


def test_repeated_offline_payload_does_not_push_duplicate_event(monkeypatch, app):
    pushed = []
    monkeypatch.setattr(
        "app.services.device_state_service.websocket_hub.push_to_user",
        lambda *args: pushed.append(args),
    )

    with app.app_context():
        user = User(wx_openid="wx-offline-repeat")
        device = Device(
            device_name="offline-repeat-device",
            owner=user,
            state="offline",
            runtime_state="idle",
            detection_state="idle",
            current_session=None,
            network_quality="unknown",
        )
        db.session.add_all([user, device])
        db.session.commit()

        app.extensions["device_coordinator"].handle_mqtt_payload(
            device.device_name,
            "online",
            {"status": "offline"},
        )

        assert pushed == []


def test_mqtt_dispatch_removes_session_after_message(monkeypatch, app):
    manager = app.extensions["device_coordinator"].mqtt
    calls = []
    original_remove = db.session.remove

    def remove():
        calls.append("remove")
        original_remove()

    monkeypatch.setattr(manager, "on_payload", lambda *args: calls.append("handled"))
    monkeypatch.setattr(db.session, "remove", remove)

    manager._dispatch("mqtt-clean-device", "online", {"status": "online"})

    assert "handled" in calls
    assert "remove" in calls


def test_mqtt_dispatch_rolls_back_and_removes_on_error(monkeypatch, app):
    manager = app.extensions["device_coordinator"].mqtt
    calls = []
    original_rollback = db.session.rollback
    original_remove = db.session.remove

    def fail_payload(*_args):
        raise RuntimeError("payload failed")

    def rollback():
        calls.append("rollback")
        original_rollback()

    def remove():
        calls.append("remove")
        original_remove()

    monkeypatch.setattr(manager, "on_payload", fail_payload)
    monkeypatch.setattr(db.session, "rollback", rollback)
    monkeypatch.setattr(db.session, "remove", remove)

    manager._dispatch("mqtt-fail-device", "online", {"status": "online"})

    assert "rollback" in calls
    assert "remove" in calls


def test_csi_seq_reset_keeps_session_and_clears_algorithm_window(app):
    app.config["CSI_WINDOW_SIZE"] = 90
    with app.app_context():
        user = User(wx_openid="wx-seq-reset")
        device = Device(
            device_name="seq-reset-device",
            owner=user,
            state="online",
            runtime_state="uploading",
            detection_state="running",
            current_session="sess-reset",
            network_quality="good",
        )
        db.session.add_all([user, device])
        db.session.commit()

        coordinator = app.extensions["device_coordinator"]
        coordinator.handle_mqtt_payload(
            device.device_name,
            "csi",
            csi_payload(
                "sess-reset",
                1,
                100,
                frame_count=45,
                batch_interval_us=1_500_000,
            ),
        )
        coordinator.handle_mqtt_payload(
            device.device_name,
            "csi",
            csi_payload(
                "sess-reset",
                2,
                145,
                frame_count=45,
                batch_interval_us=1_500_000,
            ),
        )
        coordinator.handle_mqtt_payload(
            device.device_name,
            "csi",
            csi_payload(
                "sess-reset",
                3,
                1,
                frame_count=45,
                batch_interval_us=10_000_000,
            ),
        )

        db.session.refresh(device)
        assert device.state == "online"
        assert device.runtime_state == "uploading"
        assert device.detection_state == "running"
        assert device.current_session == "sess-reset"
        assert device.network_quality == "fair"
        assert db.session.scalar(db.select(FallEvent)) is None


def test_single_csi_parse_error_does_not_stop_running_device(app):
    with app.app_context():
        user = User(wx_openid="wx-parse-error")
        device = Device(
            device_name="parse-error-device",
            owner=user,
            state="online",
            runtime_state="uploading",
            detection_state="running",
            current_session="sess-parse",
            network_quality="good",
        )
        db.session.add_all([user, device])
        db.session.commit()

        app.extensions["device_coordinator"].handle_mqtt_payload(
            device.device_name,
            "csi",
            {
                "session": "sess-parse",
                "fmt": "bad-format",
                "batch": 1,
                "frames": 45,
            },
        )

        db.session.refresh(device)
        assert device.state == "online"
        assert device.runtime_state == "uploading"
        assert device.detection_state == "running"
        assert device.current_session == "sess-parse"
        assert device.network_quality == "good"


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
        assert device.runtime_state == "fault"
        assert device.detection_state == "idle"
        assert device.current_session is None

        coordinator.handle_mqtt_payload(
            device.device_name,
            "online",
            {"status": "offline", "reason": "lwt"},
        )
        db.session.refresh(device)
        assert device.state == "error"
        assert device.runtime_state == "fault"
        assert device.fault_code == "UART_TIMEOUT"


def test_mqtt_state_csi_fall_event_and_offline_scan(client, app):
    login_data = login_miniapp(client, app)
    token = login_data["access_token"]
    user_id = login_data["user"]["id"]
    app.config["CSI_WINDOW_SIZE"] = 2
    published = []
    app.config["MQTT_CONTROL_PUBLISHER"] = (
        lambda **message: published.append(message)
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

        reset = client.post(
            "/api/v1/devices/fall-room-01/reset-fault",
            headers={
                **bearer(token),
                "Idempotency-Key": "reset-fall-room-01",
            },
        )
        assert reset.status_code == 202
        assert published[-1]["action"] == "reset"

        coordinator.handle_mqtt_payload(
            "fall-room-01",
            "ack",
            {
                "cmd": "control",
                "action": "reset",
                "ok": True,
                "state": "idle",
                "err": 0,
                "msg": "",
            },
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

        assert db.session.scalar(db.select(FallEvent)) is None
        FallAlertService(app).handle_algorithm_alert(
            device_name="fall-room-01",
            session="sess-test-001",
            network_quality=device.network_quality,
            alert=AlgorithmAlert(
                confidence=0.92,
                algorithm_class="fall",
                timestamp="2026-07-07 15:30:00",
            ),
        )

        event = db.session.scalar(db.select(FallEvent))
        assert event is not None
        assert event.status == "pending"
        assert event.notified is True
        assert event.network_quality == "good"
        assert event.algorithm_source == "docker"
        assert event.alert_count == 1

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
        device.last_csi_at = None
        device.state = "online"
        device.runtime_state = "idle"
        device.detection_state = "idle"
        device.current_session = None
        db.session.commit()
        count = app.extensions[
            "device_coordinator"
        ].scan_offline_devices()
        db.session.refresh(device)
        assert count == 1
        assert device.state == "offline"
        assert device.network_quality == "unknown"
