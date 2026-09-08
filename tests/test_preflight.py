from __future__ import annotations

from dataclasses import replace

from edgelink_agent.config import AccessPointConfig, AgentConfig
from edgelink_agent.preflight import run_preflight
from edgelink_agent.system import CommandResult


class PreflightRunner:
    def __init__(self, *, ap: bool = True) -> None:
        self.ap = ap

    def run(self, args, timeout: float = 10.0) -> CommandResult:
        command = tuple(args)
        if command[:3] == ("systemctl", "is-active", "NetworkManager.service"):
            return CommandResult(0, "active\n")
        if command[:3] == ("systemctl", "is-active", "reachy-mini-daemon.service"):
            return CommandResult(0, "active\n")
        if command[:2] == ("systemctl", "cat"):
            return CommandResult(0, "[Unit]\n")
        if command[:2] == ("busctl", "introspect"):
            return CommandResult(0, ".CheckpointCreate method aouu o\n")
        if "WIFI-PROPERTIES.AP" in command:
            return CommandResult(0, "yes\n" if self.ap else "no\n")
        if "GENERAL.STATE" in command:
            return CommandResult(0, "100 (connected)\n")
        return CommandResult(1, "", "unknown")


def test_preflight_accepts_supported_device_and_warns_about_reachy_daemon() -> None:
    config = replace(
        AgentConfig(),
        access_point=AccessPointConfig(
            connection_name="edgelink-provisioning",
            reserved_connection_names=("Hotspot",),
        ),
    )
    checks = run_preflight(config, PreflightRunner())

    assert all(check.ok for check in checks if check.severity == "error")
    warning = next(check for check in checks if check.name == "existing_network_controller")
    assert warning.severity == "warning"
    boundary = next(check for check in checks if check.name == "daemon_ap_boundary")
    assert boundary.ok
    checkpoint = next(check for check in checks if check.name == "network_checkpoint")
    assert checkpoint.ok


def test_preflight_rejects_wifi_without_ap_support() -> None:
    checks = run_preflight(AgentConfig(), PreflightRunner(ap=False))

    ap = next(check for check in checks if check.name == "ap_capability")
    assert not ap.ok
    assert ap.severity == "error"


def test_preflight_refuses_daemon_devices_without_an_explicit_hotspot_boundary() -> None:
    checks = run_preflight(AgentConfig(), PreflightRunner())

    boundary = next(check for check in checks if check.name == "daemon_ap_boundary")
    assert not boundary.ok
    assert boundary.severity == "error"
