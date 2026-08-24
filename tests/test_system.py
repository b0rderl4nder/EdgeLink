from __future__ import annotations

from edgelink_agent.config import AccessPointConfig, HealthConfig, WifiConfig
from edgelink_agent.system import (
    CommandResult,
    NetworkManagerAdapter,
    _clean_values,
    _count_connected_peers,
    _nm_split,
    _profile_name,
)


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, args, timeout: float = 10.0) -> CommandResult:
        call = tuple(args)
        self.calls.append(call)
        if call[:5] == ("nmcli", "-g", "NAME", "connection", "show"):
            return CommandResult(10, "", "not found")
        return CommandResult(0, "", "")


def test_nm_split_preserves_escaped_colons() -> None:
    assert _nm_split(r"yes:Phone\:Hotspot:87:WPA2") == ["yes", "Phone:Hotspot", "87", "WPA2"]


def test_network_manager_placeholders_are_not_treated_as_addresses() -> None:
    assert _clean_values(["--", "(none)", "192.168.1.2/24", ""]) == ["192.168.1.2/24"]


def test_easytier_peer_parser_ignores_header_and_local_node() -> None:
    output = """| ipv4 | hostname | cost | lat(ms) |
|---|---|---|---|
| 192.167.111.212/24 | reachy-mini | Local | - |
| 192.167.111.213/24 | rogerspi4 | p2p | 19.94 |
"""
    assert _count_connected_peers(output) == 1


class HotspotRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, args, timeout: float = 10.0) -> CommandResult:
        call = tuple(args)
        self.calls.append(call)
        if call[:5] == ("nmcli", "-g", "NAME", "connection", "show"):
            return CommandResult(0, "Hotspot\n")
        return CommandResult(0, "")


def test_existing_product_hotspot_is_reconciled_without_exposing_secret() -> None:
    runner = HotspotRunner()
    adapter = NetworkManagerAdapter(
        WifiConfig(),
        AccessPointConfig(connection_name="Hotspot", reuse_existing_profile=True),
        HealthConfig(),
        runner=runner,
    )

    assert adapter.start_ap("Zebao-1", "new-password")

    flattened = " ".join(part for call in runner.calls for part in call)
    assert "connection up Hotspot" in flattened
    assert "connection modify Hotspot" in flattened
    assert "802-11-wireless.ssid Zebao-1" in flattened
    assert "passwd-file" in flattened
    assert "new-password" not in flattened


def test_profile_name_never_contains_ssid_or_secret_characters() -> None:
    assert _profile_name("Customer WiFi: secret") == "edgelink-24b8ec8f9d"


def test_connect_resets_stale_static_network_settings_and_hides_password() -> None:
    runner = RecordingRunner()
    adapter = NetworkManagerAdapter(
        WifiConfig(), AccessPointConfig(), HealthConfig(), runner=runner
    )

    assert adapter.connect("Phone Hotspot", "top-secret-password")

    flattened = " ".join(part for call in runner.calls for part in call)
    assert "top-secret-password" not in flattened
    modify = next(call for call in runner.calls if "ipv4.method" in call)
    assert modify[modify.index("ipv4.method") + 1] == "auto"
    assert modify[modify.index("ipv4.addresses") + 1] == ""
    assert modify[modify.index("ipv4.gateway") + 1] == ""
    assert modify[modify.index("ipv4.dns") + 1] == ""
    assert "passwd-file" in flattened
