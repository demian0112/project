from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


logger = logging.getLogger(__name__)
FALL_CLASSES = {"fall", "fallen", "fall_down", "fall-down", "跌倒", "摔倒"}


class FallAlgorithmClientError(RuntimeError):
    """Raised for Docker fall-algorithm transport/protocol failures."""


class WebSocketTransport(Protocol):
    def send(self, payload: str) -> Any: ...

    def recv(self) -> str: ...

    def close(self) -> Any: ...


@dataclass(frozen=True, slots=True)
class AlgorithmAlert:
    confidence: float
    algorithm_class: str | None = None
    timestamp: str | None = None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class AlgorithmPong:
    raw: dict[str, Any]


class FallAlgorithmClient:
    """Protocol client for the Docker CSI fall-detection service.

    This class only knows HTTP and WebSocket protocol details. It deliberately
    does not create FallEvent rows, push mini-program WebSockets, or send WeChat
    notifications.
    """

    def __init__(
        self,
        *,
        http_base_url: str,
        ws_url: str,
        health_path: str = "/health",
        stats_path: str = "/stats",
        config_path: str = "/config",
        reset_path: str = "/reset",
        http_timeout: float = 3.0,
        ws_connect_timeout: float = 5.0,
        ws_read_timeout: float = 35.0,
        http_transport: Any | None = None,
        ws_connect: Any | None = None,
    ) -> None:
        self.http_base_url = http_base_url.rstrip("/")
        self.ws_url = ws_url
        self.health_path = health_path
        self.stats_path = stats_path
        self.config_path = config_path
        self.reset_path = reset_path
        self.http_timeout = http_timeout
        self.ws_connect_timeout = ws_connect_timeout
        self.ws_read_timeout = ws_read_timeout
        self._http_transport = http_transport or self._default_http_request
        self._ws_connect = ws_connect or self._default_ws_connect

    @classmethod
    def from_app_config(cls, config: dict[str, Any]) -> "FallAlgorithmClient":
        return cls(
            http_base_url=str(config["FALL_ALGORITHM_HTTP_BASE_URL"]),
            ws_url=str(config["FALL_ALGORITHM_WS_URL"]),
            health_path=str(config["FALL_ALGORITHM_HEALTH_PATH"]),
            stats_path=str(config["FALL_ALGORITHM_STATS_PATH"]),
            config_path=str(config["FALL_ALGORITHM_CONFIG_PATH"]),
            reset_path=str(config["FALL_ALGORITHM_RESET_PATH"]),
            http_timeout=float(config["FALL_ALGORITHM_HTTP_TIMEOUT_SECONDS"]),
            ws_connect_timeout=float(
                config["FALL_ALGORITHM_WS_CONNECT_TIMEOUT_SECONDS"]
            ),
            ws_read_timeout=float(config["FALL_ALGORITHM_WS_READ_TIMEOUT_SECONDS"]),
        )

    def health_check(self) -> dict[str, Any]:
        payload = self._request_json("GET", self.health_path)
        if payload.get("status") != "ok":
            raise FallAlgorithmClientError("fall algorithm health is not ok")
        return payload

    def get_stats(self) -> dict[str, Any]:
        return self._request_json("GET", self.stats_path)

    def get_config(self) -> dict[str, Any]:
        return self._request_json("GET", self.config_path)

    def update_config(self, config: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("POST", self.config_path, payload=config)

    def reset(self) -> dict[str, Any]:
        return self._request_json("POST", self.reset_path, payload={})

    def connect_stream(self) -> WebSocketTransport:
        try:
            return self._ws_connect(
                self.ws_url,
                timeout=self.ws_connect_timeout,
                read_timeout=self.ws_read_timeout,
            )
        except Exception as exc:
            raise FallAlgorithmClientError("failed to connect algorithm stream") from exc

    def send_data(self, websocket: WebSocketTransport, line: str) -> None:
        self._send_json(websocket, {"type": "data", "line": line})

    def send_ping(self, websocket: WebSocketTransport) -> None:
        self._send_json(websocket, {"type": "ping"})

    def receive(
        self,
        websocket: WebSocketTransport,
    ) -> AlgorithmAlert | AlgorithmPong | None:
        try:
            raw = websocket.recv()
        except TimeoutError:
            return None
        except Exception as exc:
            if exc.__class__.__name__ == "WebSocketTimeoutException":
                return None
            raise
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            logger.debug("Ignored malformed algorithm WebSocket JSON")
            return None
        if not isinstance(payload, dict):
            logger.debug("Ignored non-object algorithm WebSocket payload")
            return None

        message_type = payload.get("type")
        if message_type == "pong":
            return AlgorithmPong(raw=payload)
        if message_type != "alert":
            logger.debug(
                "Ignored algorithm WebSocket message type=%s",
                message_type,
            )
            return None

        return self._parse_alert(payload)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self._url(path)
        try:
            response = self._http_transport(
                method,
                url,
                payload,
                self.http_timeout,
            )
        except FallAlgorithmClientError:
            raise
        except Exception as exc:
            raise FallAlgorithmClientError(
                f"fall algorithm HTTP {method} {path} failed"
            ) from exc
        if not isinstance(response, dict):
            raise FallAlgorithmClientError("fall algorithm returned invalid JSON")
        return response

    def _url(self, path: str) -> str:
        return f"{self.http_base_url}/{str(path).lstrip('/')}"

    def _send_json(
        self,
        websocket: WebSocketTransport,
        payload: dict[str, Any],
    ) -> None:
        try:
            websocket.send(json.dumps(payload, separators=(",", ":")))
        except Exception as exc:
            raise FallAlgorithmClientError("failed to send algorithm WebSocket data") from exc

    def _parse_alert(self, payload: dict[str, Any]) -> AlgorithmAlert | None:
        if payload.get("is_fall") is not True:
            logger.debug("Ignored non-fall algorithm alert")
            return None
        try:
            confidence = float(payload.get("confidence"))
        except (TypeError, ValueError):
            logger.debug("Ignored algorithm alert with invalid confidence")
            return None
        if confidence < 0.0 or confidence > 1.0:
            logger.debug("Ignored algorithm alert with out-of-range confidence")
            return None

        raw_class = payload.get("class")
        algorithm_class = str(raw_class).strip() if raw_class is not None else None
        if algorithm_class:
            normalized = algorithm_class.lower().replace(" ", "_")
            if normalized not in FALL_CLASSES:
                logger.debug(
                    "Ignored algorithm alert with non-fall class=%s",
                    algorithm_class,
                )
                return None

        timestamp = payload.get("timestamp")
        return AlgorithmAlert(
            confidence=confidence,
            algorithm_class=algorithm_class,
            timestamp=str(timestamp).strip() if timestamp else None,
            raw=payload,
        )

    def _default_http_request(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None,
        timeout: float,
    ) -> dict[str, Any]:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310
                raw = response.read().decode("utf-8").strip()
        except (HTTPError, URLError, TimeoutError) as exc:
            raise FallAlgorithmClientError(f"HTTP {method} {url} failed") from exc
        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FallAlgorithmClientError("fall algorithm returned malformed JSON") from exc
        if not isinstance(decoded, dict):
            raise FallAlgorithmClientError("fall algorithm returned non-object JSON")
        return decoded

    def _default_ws_connect(
        self,
        url: str,
        *,
        timeout: float,
        read_timeout: float,
    ) -> WebSocketTransport:
        try:
            from websocket import create_connection
        except ImportError as exc:
            raise FallAlgorithmClientError(
                "websocket-client is required for FALL_ALGORITHM_WS_URL"
            ) from exc
        websocket = create_connection(url, timeout=timeout)
        websocket.settimeout(read_timeout)
        return websocket
