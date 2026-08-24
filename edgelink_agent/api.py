from __future__ import annotations

import ipaddress
import json
import logging
import threading
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any
from urllib.parse import unquote, urlsplit

from . import __version__
from .config import AgentConfig, access_point_ssid
from .controller import EdgeLinkController
from .ports import NetworkPort

LOGGER = logging.getLogger("edgelink.api")
MAX_BODY_BYTES = 8192


class _LocalThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16
    allow_reuse_address = True


class ApiServer:
    def __init__(
        self,
        config: AgentConfig,
        controller: EdgeLinkController,
        network: NetworkPort,
    ) -> None:
        self._config = config
        handler = _handler_factory(config, controller, network)
        self._server = _LocalThreadingHTTPServer(
            (config.api.listen_host, config.api.listen_port), handler
        )
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="edgelink-api", daemon=True
        )
        self._thread.start()
        LOGGER.info(
            "API listening on %s:%s", self._config.api.listen_host, self._config.api.listen_port
        )

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=3.0)


def _handler_factory(
    config: AgentConfig,
    controller: EdgeLinkController,
    network: NetworkPort,
) -> type[BaseHTTPRequestHandler]:
    trusted = tuple(ipaddress.ip_network(item) for item in config.api.trusted_mutation_subnets)

    class Handler(BaseHTTPRequestHandler):
        server_version = "EdgeLink/0.1"

        def log_message(self, format_string: str, *args: object) -> None:
            path = urlsplit(self.path).path
            if path.startswith("/v1/wifi/profiles/"):
                path = "/v1/wifi/profiles/<redacted>"
            quiet = path in {
                "/healthz",
                "/v1/status",
                "/v1/provisioning/activity",
                "/v1/events/network",
            }
            log = LOGGER.debug if quiet else LOGGER.info
            status = str(args[1]) if len(args) > 1 else ""
            log(
                "client=%s method=%s path=%s status=%s",
                self.client_address[0],
                self.command,
                path,
                status,
            )

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/":
                if self._provisioning_client(trusted):
                    controller.note_provisioning_activity()
                self._html(INDEX_HTML.replace("{{DEVICE_NAME}}", _html_escape(config.device_name)))
                return
            if path in {"/healthz", "/v1/status"}:
                status = controller.status().as_dict()
                self._json(
                    HTTPStatus.OK,
                    {"ok": True, "version": __version__, "build": _build_info(), **status},
                )
                return
            if path == "/v1/wifi/scan":
                controller.note_provisioning_activity()
                self._json(
                    HTTPStatus.OK,
                    {"ok": True, "networks": [asdict(item) for item in network.scan()]},
                )
                return
            if path == "/v1/config/public":
                self._json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "device_name": config.device_name,
                        "ap_ssid": access_point_ssid(config),
                        "ap_ssid_prefix": config.access_point.ssid_prefix,
                    },
                )
                return
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if not self._mutation_allowed(trusted):
                self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "local_access_only"})
                return
            if path == "/v1/events/network":
                if not self._loopback_client():
                    self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "loopback_only"})
                    return
                controller.notify_network_event()
                self._json(HTTPStatus.ACCEPTED, {"ok": True})
                return
            if path == "/v1/provisioning/activity":
                controller.note_provisioning_activity()
                self._json(HTTPStatus.ACCEPTED, {"ok": True})
                return
            if path == "/v1/wifi/connect":
                controller.note_provisioning_activity()
                payload = self._read_json()
                if payload is None:
                    return
                raw_ssid = payload.get("ssid", "")
                raw_password = payload.get("password", "")
                if not isinstance(raw_ssid, str) or not isinstance(raw_password, str):
                    self._json(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"ok": False, "error": "invalid_wifi_credentials"},
                    )
                    return
                ssid = raw_ssid
                password = raw_password
                ok, reason, operation_id = controller.request_wifi_provisioning(ssid, password)
                if ok:
                    response_status = HTTPStatus.ACCEPTED
                elif reason == "provisioning_busy":
                    response_status = HTTPStatus.CONFLICT
                else:
                    response_status = HTTPStatus.UNPROCESSABLE_ENTITY
                self._json(
                    response_status,
                    {
                        "ok": ok,
                        "accepted": ok,
                        "operation_id": operation_id,
                        "ssid": ssid,
                        "reason": reason,
                    },
                )
                return
            actions = {
                "/v1/ap/start": controller.start_ap,
                "/v1/network/recover": controller.recover_now,
            }
            if path == "/v1/ap/stop":
                if not self._loopback_client():
                    self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "loopback_only"})
                    return
                ok = controller.stop_ap()
                self._json(HTTPStatus.OK if ok else HTTPStatus.BAD_GATEWAY, {"ok": ok})
                return
            action = actions.get(path)
            if action is not None:
                ok = action()
                self._json(
                    HTTPStatus.OK if ok else HTTPStatus.BAD_GATEWAY,
                    {"ok": ok},
                )
                return
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

        def do_DELETE(self) -> None:
            path = urlsplit(self.path).path
            if not self._loopback_client():
                self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "loopback_only"})
                return
            prefix = "/v1/wifi/profiles/"
            if path.startswith(prefix):
                profile = unquote(path[len(prefix) :])
                ok = network.forget(profile)
                self._json(
                    HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST,
                    {"ok": ok},
                )
                return
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

        def _mutation_allowed(
            self, networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
        ) -> bool:
            try:
                address = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                return False
            return any(address in network for network in networks)

        def _provisioning_client(
            self, networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
        ) -> bool:
            try:
                address = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                return False
            return not address.is_loopback and any(address in network for network in networks)

        def _loopback_client(self) -> bool:
            try:
                return ipaddress.ip_address(self.client_address[0]).is_loopback
            except ValueError:
                return False

        def _read_json(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY_BYTES:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_body"})
                return None
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_json"})
                return None
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_json"})
                return None
            return payload

        def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                LOGGER.debug("client disconnected before JSON response was written")

        def _html(self, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(HTTPStatus.OK.value)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                LOGGER.debug("client disconnected before HTML response was written")

    return Handler


def _html_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_info() -> dict[str, str]:
    path = files("edgelink_agent").joinpath("../BUILD_INFO.json")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"commit": "development"}
    if not isinstance(raw, dict):
        return {"commit": "unknown"}
    return {
        "commit": str(raw.get("commit", "unknown")),
        "version": str(raw.get("version", __version__)),
    }


INDEX_HTML = files("edgelink_agent").joinpath("web/index.html").read_text(encoding="utf-8")
