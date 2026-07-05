from __future__ import annotations

import json
import threading
from collections import defaultdict
from typing import Any

from flask import request

from ..extensions import db, sock
from ..models import User
from .token_service import AccessTokenError, decode_access_token


class WebSocketHub:
    def __init__(self) -> None:
        self._connections: dict[int, set[Any]] = defaultdict(set)
        self._lock = threading.RLock()

    def add(self, user_id: int, websocket: Any) -> None:
        with self._lock:
            self._connections[user_id].add(websocket)

    def remove(self, user_id: int, websocket: Any) -> None:
        with self._lock:
            connections = self._connections.get(user_id)
            if connections is None:
                return
            connections.discard(websocket)
            if not connections:
                self._connections.pop(user_id, None)

    def push_to_user(
        self,
        user_id: int,
        event: str,
        device_name: str | None,
        data: dict[str, Any],
    ) -> int:
        message = json.dumps(
            {
                "event": event,
                "device_name": device_name,
                "data": data,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock:
            connections = list(self._connections.get(user_id, ()))

        delivered = 0
        for websocket in connections:
            try:
                websocket.send(message)
                delivered += 1
            except Exception:
                self.remove(user_id, websocket)
        return delivered


websocket_hub = WebSocketHub()


def register_websocket_routes(app) -> None:
    @sock.route("/ws/v1/events")
    def miniapp_events(websocket):
        token = request.args.get("token", "")
        try:
            user_id = decode_access_token(token)
        except AccessTokenError:
            websocket.send(
                json.dumps(
                    {
                        "error": "AUTH_REQUIRED",
                        "message": "登录状态无效或已过期",
                    },
                    ensure_ascii=False,
                )
            )
            websocket.close()
            return

        user = db.session.get(User, user_id)
        if user is None or user.status != "active":
            websocket.send(
                json.dumps(
                    {
                        "error": "AUTH_REQUIRED",
                        "message": "用户不存在或已被禁用",
                    },
                    ensure_ascii=False,
                )
            )
            websocket.close()
            return

        websocket_hub.add(user_id, websocket)
        websocket.send(
            json.dumps(
                {"event": "connection.ready", "data": {"user_id": user_id}},
                separators=(",", ":"),
            )
        )
        try:
            while websocket.receive() is not None:
                pass
        finally:
            websocket_hub.remove(user_id, websocket)

    # Keep a reference for tests and application services.
    app.extensions["websocket_hub"] = websocket_hub
