from __future__ import annotations

from edgelink_agent.config import AgentConfig
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
        if "WIFI-PROPERTIES.AP" in command:
            return CommandResult(0, "yes\n" if self.ap else "no\n")
        if "GENERAL.STATE" in command:
            return CommandResult(0, "100 (connected)\n")
        return CommandResult(1, "", "unknown")


def test_preflight_accepts_supported_device_and_warns_about_reachy_daemon() -> None:
    checks = run_preflight(AgentConfig(), PreflightRunner())

    assert all(check.ok for check in checks if check.severity == "error")
    warning = next(check for check in checks if check.name == "existing_network_controller")
    assert warning.severity == "warning"


def test_preflight_rejects_wifi_without_ap_support() -> None:
    checks = run_preflight(AgentConfig(), PreflightRunner(ap=False))

    ap = next(check for check in checks if check.name == "ap_capability")
    assert not ap.ok
    assert ap.severity == "error"
