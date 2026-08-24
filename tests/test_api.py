from __future__ import annotations

import json
import urllib.error
import urllib.request

from edgelink_agent.api import ApiServer
from edgelink_agent.config import AgentConfig, ApiConfig, OverlayConfig
from edgelink_agent.controller import EdgeLinkController
from edgelink_agent.models import NetworkSnapshot, OverlaySnapshot, WifiNetwork


class ApiNetwork:
    def snapshot(self) -> NetworkSnapshot:
        return NetworkSnapshot(interface="wlan0", ssid="Test WiFi")

    def scan(self) -> list[WifiNetwork]:
        return [WifiNetwork("Test WiFi", 90, "WPA2", True)]

    def reconnect_saved(self) -> bool:
        return True

    def connect(self, ssid: str, password: str) -> bool:
        return False

    def forget(self, connection_name: str) -> bool:
        return True

    def start_ap(self, ssid: str, password: str) -> bool:
        return True

    def stop_ap(self) -> bool:
        return True


class ApiOverlay:
    def snapshot(self) -> OverlaySnapshot:
        return OverlaySnapshot(enabled=False, connected=True)

    def start(self) -> bool:
        return True

    def restart(self) -> bool:
        return True


def _json_request(url: str, method: str = "GET", payload: dict[str, str] | None = None):
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        method=method,
        data=body,
        headers={"Content-Type": "application/json"} if body else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_http_status_scan_and_secret_free_failed_connect() -> None:
    config = AgentConfig(
        device_name="test-device",
        overlay=OverlayConfig(enabled=False),
        api=ApiConfig(listen_host="127.0.0.1", listen_port=0),
    )
    network = ApiNetwork()
    controller = EdgeLinkController(config, network, ApiOverlay())
    controller.tick()
    server = ApiServer(config, controller, network)
    server.start()
    base = f"http://127.0.0.1:{server.address[1]}"
    try:
        status_code, status = _json_request(f"{base}/v1/status")
        assert status_code == 200
        assert status["version"] == "0.1.2"
        assert "commit" in status["build"]
        assert status["network"]["ssid"] == "Test WiFi"

        status_code, scan = _json_request(f"{base}/v1/wifi/scan")
        assert status_code == 200
        assert scan["networks"][0]["ssid"] == "Test WiFi"

        status_code, public_config = _json_request(f"{base}/v1/config/public")
        assert status_code == 200
        assert public_config["ap_ssid"] == "EdgeLink-test-device"

        status_code, failure = _json_request(
            f"{base}/v1/wifi/connect",
            "POST",
            {"ssid": "Another WiFi", "password": "never-return-this"},
        )
        assert status_code == 502
        assert failure == {
            "ok": False,
            "ssid": "Another WiFi",
            "reason": "connection_failed",
        }
        assert "never-return-this" not in json.dumps(failure)

        status_code, activity = _json_request(
            f"{base}/v1/provisioning/activity", "POST"
        )
        assert status_code == 202
        assert activity == {"ok": True}
    finally:
        server.close()
