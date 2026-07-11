import threading

from sqlalchemy import func

from app.extensions import db
from app.models import Device, FallEvent, User
from app.services.fall_alert_service import FallAlertService
from app.services.fall_algorithm_client import AlgorithmAlert


def create_alert_device():
    user = User(wx_openid="wx-alert-owner")
    device = Device(
        device_name="alert-device",
        display_name="Alert Device",
        owner=user,
        state="online",
        runtime_state="uploading",
        detection_state="running",
        current_session="sess-alert",
        network_quality="good",
    )
    db.session.add_all([user, device])
    db.session.commit()
    return device.id


def test_alert_creates_pending_then_aggregates_until_handled(monkeypatch, app):
    pushed = []
    wechat = []
    monkeypatch.setattr(
        "app.services.fall_alert_service.websocket_hub.push_to_user",
        lambda *args: pushed.append(args) or 1,
    )
    monkeypatch.setattr(
        "app.services.fall_alert_service.send_fall_alert",
        lambda event_id, triggered_by: wechat.append(
            (event_id, triggered_by)
        )
        or {"ok": True, "sent": True},
    )

    with app.app_context():
        create_alert_device()
        service = FallAlertService(app)

        first = service.handle_algorithm_alert(
            device_name="alert-device",
            session="sess-alert",
            network_quality="good",
            alert=AlgorithmAlert(
                confidence=0.8,
                algorithm_class="fall",
                timestamp="2026-07-07 15:30:00",
            ),
        )
        second = service.handle_algorithm_alert(
            device_name="alert-device",
            session="sess-alert",
            network_quality="fair",
            alert=AlgorithmAlert(confidence=0.95, algorithm_class="fall"),
        )

        event = db.session.get(FallEvent, first["fall_event_id"])
        assert first["created"] is True
        assert second["created"] is False
        assert event.alert_count == 2
        assert event.max_confidence == 0.95
        assert event.network_quality == "fair"
        assert len(pushed) == 1
        assert len(wechat) == 1

        event.status = "confirmed"
        db.session.commit()
        third = service.handle_algorithm_alert(
            device_name="alert-device",
            session="sess-alert-2",
            network_quality="good",
            alert=AlgorithmAlert(confidence=0.9, algorithm_class="fall"),
        )

        assert third["created"] is True
        assert db.session.scalar(db.select(func.count(FallEvent.id))) == 2
        assert len(pushed) == 2
        assert len(wechat) == 2


def test_concurrent_alerts_do_not_create_duplicate_pending(monkeypatch, app):
    monkeypatch.setattr(
        "app.services.fall_alert_service.websocket_hub.push_to_user",
        lambda *_args: 1,
    )
    monkeypatch.setattr(
        "app.services.fall_alert_service.send_fall_alert",
        lambda *_args, **_kwargs: {"ok": True, "sent": False},
    )
    with app.app_context():
        create_alert_device()

    service = FallAlertService(app)
    barrier = threading.Barrier(2)
    results = []

    def worker(confidence):
        barrier.wait()
        results.append(
            service.handle_algorithm_alert(
                device_name="alert-device",
                session="sess-alert",
                network_quality="good",
                alert=AlgorithmAlert(confidence=confidence, algorithm_class="fall"),
            )
        )

    threads = [
        threading.Thread(target=worker, args=(0.81,)),
        threading.Thread(target=worker, args=(0.91,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with app.app_context():
        events = db.session.scalars(db.select(FallEvent)).all()
        assert len(events) == 1
        assert events[0].alert_count == 2
        assert events[0].max_confidence == 0.91
        assert {result["created"] for result in results} == {True, False}
