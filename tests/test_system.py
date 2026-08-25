from __future__ import annotations

from edgelink_agent.config import AccessPointConfig, HealthConfig, WifiConfig
from edgelink_agent.system import (
    CommandResult,
    ConnectivityProbe,
    NetworkManagerAdapter,
    _clean_values,
    _count_connected_peers,
    _nm_split,
    _profile_name,
)


class ProbeRunner:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def run(self, args, timeout: float = 10.0) -> CommandResult:
        self.calls.append((tuple(args), timeout))
        return self.result


def test_dns_probe_is_bounded_by_configured_timeout() -> None:
    runner = ProbeRunner(CommandResult(124, "", "TimeoutExpired"))
    probe = ConnectivityProbe(HealthConfig(dns_timeout_seconds=1.25), runner)

    assert not probe.dns_available()
    assert runner.calls == [(('getent', 'ahostsv4', 'example.com'), 1.25)]


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
        AccessPointConfig(connection_name="Hotspot"),
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
    assert modify[modify.index("connection.autoconnect") + 1] == "no"
    assert modify[modify.index("connection.autoconnect-retries") + 1] == "1"
    assert "passwd-file" in flattened


def test_suspending_failed_profile_prevents_it_from_stealing_ap() -> None:
    runner = RecordingRunner()
    adapter = NetworkManagerAdapter(
        WifiConfig(), AccessPointConfig(), HealthConfig(), runner=runner
    )

    assert adapter.suspend("Unavailable WiFi")

    modify = runner.calls[-1]
    assert modify[:4] == ("nmcli", "connection", "modify", _profile_name("Unavailable WiFi"))
    assert modify[-2:] == ("connection.autoconnect", "no")


def test_committing_verified_profile_enables_bounded_autoconnect() -> None:
    runner = RecordingRunner()
    adapter = NetworkManagerAdapter(
        WifiConfig(), AccessPointConfig(), HealthConfig(), runner=runner
    )

    assert adapter.commit("Verified WiFi")

    modify = runner.calls[-1]
    assert modify[modify.index("connection.autoconnect") + 1] == "yes"
    assert modify[modify.index("connection.autoconnect-priority") + 1] == "20"
    assert modify[modify.index("connection.autoconnect-retries") + 1] == "1"


def test_restore_activates_exact_previous_connection() -> None:
    class WifiProfileRunner(RecordingRunner):
        def run(self, args, timeout: float = 10.0) -> CommandResult:
            call = tuple(args)
            self.calls.append(call)
            if call[:4] == (
                "nmcli",
                "-g",
                "connection.type,connection.interface-name",
                "connection",
            ):
                return CommandResult(0, "802-11-wireless\nwlan0\n", "")
            return CommandResult(0, "", "")

    runner = WifiProfileRunner()
    adapter = NetworkManagerAdapter(
        WifiConfig(), AccessPointConfig(), HealthConfig(), runner=runner
    )

    assert adapter.restore("Office LAN")

    assert runner.calls[-1] == (
        "nmcli",
        "--wait",
        "20",
        "connection",
        "up",
        "Office LAN",
    )


class CheckpointRunner(RecordingRunner):
    def run(self, args, timeout: float = 10.0) -> CommandResult:
        call = tuple(args)
        self.calls.append(call)
        if call[:5] == ("nmcli", "-g", "GENERAL.DBUS-PATH", "device", "show"):
            return CommandResult(0, "/org/freedesktop/NetworkManager/Devices/3\n")
        if "CheckpointCreate" in " ".join(call):
            return CommandResult(
                0,
                "(objectpath '/org/freedesktop/NetworkManager/Checkpoint/7',)\n",
            )
        return CommandResult(0, "()\n")


def test_network_manager_checkpoint_is_scoped_without_global_profile_deletion() -> None:
    runner = CheckpointRunner()
    adapter = NetworkManagerAdapter(
        WifiConfig(), AccessPointConfig(), HealthConfig(), runner=runner
    )

    checkpoint = adapter.begin_checkpoint(90)

    assert checkpoint == "/org/freedesktop/NetworkManager/Checkpoint/7"
    create = next(call for call in runner.calls if "CheckpointCreate" in " ".join(call))
    assert "['/org/freedesktop/NetworkManager/Devices/3']" in create
    assert create[-2:] == ("90", "0")
    assert adapter.rollback_checkpoint(checkpoint)
    assert adapter.commit_checkpoint(checkpoint)


class ExistingProfileRunner(RecordingRunner):
    def __init__(self, connection_type: str, interface_name: str) -> None:
        super().__init__()
        self.connection_type = connection_type
        self.interface_name = interface_name

    def run(self, args, timeout: float = 10.0) -> CommandResult:
        call = tuple(args)
        self.calls.append(call)
        if call[:4] == (
            "nmcli",
            "-g",
            "connection.type,connection.interface-name",
            "connection",
        ):
            return CommandResult(
                0,
                f"{self.connection_type}\n{self.interface_name}\n",
                "",
            )
        return CommandResult(0, "", "")


def test_forget_refuses_wired_and_non_owned_profiles() -> None:
    wired = ExistingProfileRunner("802-3-ethernet", "eth0")
    adapter = NetworkManagerAdapter(
        WifiConfig(), AccessPointConfig(), HealthConfig(), runner=wired
    )

    assert not adapter.forget("edgelink-wired-profile")
    assert not adapter.forget("Wired connection 1")
    assert not any(call[:3] == ("nmcli", "connection", "delete") for call in wired.calls)


def test_forget_allows_only_owned_wifi_profile_on_configured_interface() -> None:
    wifi = ExistingProfileRunner("802-11-wireless", "wlan0")
    adapter = NetworkManagerAdapter(
        WifiConfig(), AccessPointConfig(), HealthConfig(), runner=wifi
    )

    assert adapter.forget("edgelink-0123456789")
    assert wifi.calls[-1] == (
        "nmcli",
        "connection",
        "delete",
        "edgelink-0123456789",
    )


def test_restore_refuses_wired_profile_even_when_name_is_supplied() -> None:
    wired = ExistingProfileRunner("802-3-ethernet", "eth0")
    adapter = NetworkManagerAdapter(
        WifiConfig(), AccessPointConfig(), HealthConfig(), runner=wired
    )

    assert not adapter.restore("Wired connection 1")
    assert not any(call[:3] == ("nmcli", "--wait", "20") for call in wired.calls)


class ActivationFailureRunner(RecordingRunner):
    def __init__(self, reason: int, stderr: str = "") -> None:
        super().__init__()
        self.reason = reason
        self.stderr = stderr

    def run(self, args, timeout: float = 10.0) -> CommandResult:
        call = tuple(args)
        self.calls.append(call)
        if call[:5] == ("nmcli", "-g", "NAME", "connection", "show"):
            return CommandResult(10, "", "not found")
        if "connection" in call and "up" in call:
            return CommandResult(4, "", self.stderr)
        if call[:5] == ("nmcli", "-g", "GENERAL.REASON", "device", "show"):
            return CommandResult(0, f"{self.reason} (redacted)\n")
        if call[:5] == ("nmcli", "-t", "-f", "SSID", "device"):
            return CommandResult(0, "Slow Network\nPrivate Network\n")
        return CommandResult(0, "", "")


def test_activation_failures_use_networkmanager_reason_codes_without_leaking_secret() -> None:
    cases = {
        53: "ssid_not_found",
        7: "authentication_failed",
        17: "dhcp_failed",
        64: "address_conflict",
        75: "interface_unmanaged",
    }
    for code, expected in cases.items():
        runner = ActivationFailureRunner(code)
        adapter = NetworkManagerAdapter(
            WifiConfig(), AccessPointConfig(), HealthConfig(), runner=runner
        )
        result = adapter.connect("Private Network", "top-secret-password")
        assert not result
        assert result.reason == expected
        assert "top-secret-password" not in " ".join(
            part for call in runner.calls for part in call
        )


def test_timeout_is_classified_when_networkmanager_has_no_specific_reason() -> None:
    adapter = NetworkManagerAdapter(
        WifiConfig(),
        AccessPointConfig(),
        HealthConfig(),
        runner=ActivationFailureRunner(0, "TimeoutExpired"),
    )
    result = adapter.connect("Slow Network", "12345678")
    assert result.reason == "activation_timeout"


def test_timed_out_ssid_absent_from_cached_scan_is_classified_as_not_found() -> None:
    adapter = NetworkManagerAdapter(
        WifiConfig(),
        AccessPointConfig(),
        HealthConfig(),
        runner=ActivationFailureRunner(0, "TimeoutExpired"),
    )
    result = adapter.connect("Definitely Missing", "12345678")
    assert result.reason == "ssid_not_found"


class HealthRunner(RecordingRunner):
    def __init__(self, dns_ok: bool) -> None:
        super().__init__()
        self.dns_ok = dns_ok

    def run(self, args, timeout: float = 10.0) -> CommandResult:
        call = tuple(args)
        self.calls.append(call)
        device_fields = (
            "GENERAL.STATE,GENERAL.CONNECTION,GENERAL.CON-UUID,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS"
        )
        if call[:3] == ("nmcli", "-f", device_fields):
            return CommandResult(
                0,
                "GENERAL.STATE: 100 (connected)\n"
                "GENERAL.CONNECTION: Office\n"
                "GENERAL.CON-UUID: 12345678-1234-1234-1234-123456789abc\n"
                "IP4.ADDRESS[1]: 192.168.1.20/24\n"
                "IP4.GATEWAY: 192.168.1.1\n"
                "IP4.DNS[1]: 192.168.1.1\n",
            )
        if call[:3] == ("nmcli", "-g", "802-11-wireless.mode"):
            return CommandResult(0, "infrastructure\n")
        if call[:5] == ("nmcli", "-t", "-f", "ACTIVE,SSID", "device"):
            return CommandResult(0, "yes:Office\n")
        if call[:3] == ("nmcli", "networking", "connectivity"):
            return CommandResult(0, "unknown\n")
        if call and call[0] == "getent":
            return CommandResult(0 if self.dns_ok else 2, "1.2.3.4\n" if self.dns_ok else "")
        return CommandResult(0, "")


class SocketConnection:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def test_target_probe_is_independent_of_dns_probe(monkeypatch) -> None:
    monkeypatch.setattr("socket.create_connection", lambda *args, **kwargs: SocketConnection())
    adapter = NetworkManagerAdapter(
        WifiConfig(), AccessPointConfig(), HealthConfig(), runner=HealthRunner(False)
    )

    snapshot = adapter.snapshot()

    assert not snapshot.dns_reachable
    assert snapshot.target_reachable
    assert snapshot.health_level == "internet"
    assert snapshot.uplink_ready
    assert snapshot.error == "dns_degraded"


def test_dns_can_be_one_of_several_independent_internet_signals(monkeypatch) -> None:
    def unreachable(*args, **kwargs):
        raise OSError("blocked target")

    monkeypatch.setattr("socket.create_connection", unreachable)
    adapter = NetworkManagerAdapter(
        WifiConfig(), AccessPointConfig(), HealthConfig(), runner=HealthRunner(True)
    )

    snapshot = adapter.snapshot()

    assert snapshot.dns_reachable
    assert not snapshot.target_reachable
    assert snapshot.uplink_ready
    assert snapshot.health_level == "internet"


def test_strict_health_policy_can_require_non_dns_internet_evidence(monkeypatch) -> None:
    def unreachable(*args, **kwargs):
        raise OSError("blocked target")

    monkeypatch.setattr("socket.create_connection", unreachable)
    health = HealthConfig(accept_dns_as_internet_evidence=False)
    adapter = NetworkManagerAdapter(
        WifiConfig(), AccessPointConfig(), health, runner=HealthRunner(True)
    )

    snapshot = adapter.snapshot()

    assert not snapshot.uplink_ready
    assert snapshot.health_level == "local"
    assert snapshot.error == "uplink_target_unreachable"


class DuplicateHotspotRunner(RecordingRunner):
    ACTIVE_UUID = "0436d1e1-c4e7-450f-ba01-0d6a5d9522d3"

    def run(self, args, timeout: float = 10.0) -> CommandResult:
        call = tuple(args)
        self.calls.append(call)
        if call[:2] == ("nmcli", "-f") and "device" in call:
            return CommandResult(
                0,
                "GENERAL.STATE: 100 (connected)\n"
                "GENERAL.CONNECTION: Hotspot\n"
                f"GENERAL.CON-UUID: {self.ACTIVE_UUID}\n"
                "IP4.ADDRESS[1]: 10.42.0.1/24\n"
                "IP4.GATEWAY: --\n"
                "IP4.DNS[1]: --\n",
                "",
            )
        if call[:3] == ("nmcli", "-g", "802-11-wireless.mode"):
            identifier = call[-1]
            if identifier == self.ACTIVE_UUID:
                return CommandResult(0, "ap\n", "")
            return CommandResult(2, "", "ambiguous connection name")
        if call[:3] == ("nmcli", "networking", "connectivity"):
            return CommandResult(0, "full\n", "")
        return CommandResult(0, "", "")


def test_snapshot_uses_active_uuid_when_duplicate_hotspot_names_exist() -> None:
    runner = DuplicateHotspotRunner()
    adapter = NetworkManagerAdapter(
        WifiConfig(),
        AccessPointConfig(
            connection_name="edgelink-provisioning",
            reserved_connection_names=("Hotspot",),
        ),
        HealthConfig(),
        runner=runner,
    )

    snapshot = adapter.snapshot()

    assert snapshot.connection_name == "Hotspot"
    assert snapshot.ap_active
    mode_call = next(call for call in runner.calls if "802-11-wireless.mode" in call)
    assert mode_call[-1] == DuplicateHotspotRunner.ACTIVE_UUID
