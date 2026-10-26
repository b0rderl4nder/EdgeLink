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
    AP_UUID = "11111111-1111-4111-8111-111111111111"

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, args, timeout: float = 10.0) -> CommandResult:
        call = tuple(args)
        self.calls.append(call)
        if call[:4] == ("nmcli", "-t", "-f", "UUID,TYPE,NAME"):
            return CommandResult(0, f"{self.AP_UUID}:802-11-wireless:Hotspot\n")
        if "connection.id,connection.interface-name,802-11-wireless.mode" in call:
            return CommandResult(0, "Hotspot\nwlan0\nap\n")
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
    assert f"connection up {runner.AP_UUID}" in flattened
    assert f"connection modify {runner.AP_UUID}" in flattened
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


class SavedReconnectRunner(RecordingRunner):
    def __init__(self, *, visible: bool = True) -> None:
        super().__init__()
        self.visible = visible

    def run(self, args, timeout: float = 10.0) -> CommandResult:
        call = tuple(args)
        self.calls.append(call)
        if call[:5] == ("nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL,SECURITY", "device"):
            output = "yes:Zebao-1:0:WPA2\n"
            if self.visible:
                output += "no:LZWC:99:WPA2\n"
            return CommandResult(0, output)
        if call[:6] == (
            "nmcli",
            "-t",
            "-f",
            "UUID,TYPE,AUTOCONNECT,AUTOCONNECT-PRIORITY",
            "connection",
            "show",
        ):
            return CommandResult(
                0,
                "daemon-ap:802-11-wireless:yes:100\n"
                "office-wifi:802-11-wireless:yes:10\n",
            )
        if call[-3:] == ("connection", "show", "daemon-ap"):
            return CommandResult(0, "Hotspot\nwlan0\nap\nZebao-1\n")
        if call[-3:] == ("connection", "show", "office-wifi"):
            return CommandResult(0, "LZWC\nwlan0\ninfrastructure\nLZWC\n")
        return CommandResult(0, "")


def test_reconnect_saved_selects_visible_non_ap_profile_by_uuid() -> None:
    runner = SavedReconnectRunner()
    adapter = NetworkManagerAdapter(
        WifiConfig(),
        AccessPointConfig(
            connection_name="edgelink-provisioning",
            reserved_connection_names=("Hotspot",),
        ),
        HealthConfig(),
        runner=runner,
    )

    assert adapter.reconnect_saved()

    assert runner.calls[-2] == (
        "nmcli",
        "--wait",
        "10",
        "device",
        "disconnect",
        "wlan0",
    )
    assert runner.calls[-1] == (
        "nmcli",
        "--wait",
        "20",
        "connection",
        "up",
        "uuid",
        "office-wifi",
    )


def test_reconnect_saved_without_visible_infrastructure_keeps_ap_untouched() -> None:
    runner = SavedReconnectRunner(visible=False)
    adapter = NetworkManagerAdapter(
        WifiConfig(), AccessPointConfig(), HealthConfig(), runner=runner
    )

    assert not adapter.reconnect_saved()
    assert not any("disconnect" in call for call in runner.calls)


def test_excluded_unverified_wifi_cannot_steal_recovery_ap() -> None:
    runner = SavedReconnectRunner()
    adapter = NetworkManagerAdapter(
        WifiConfig(), AccessPointConfig(), HealthConfig(), runner=runner
    )

    assert not adapter.reconnect_saved(frozenset({"LZWC"}))
    assert not any("disconnect" in call for call in runner.calls)
    assert not any(call[:4] == ("nmcli", "--wait", "20", "connection") for call in runner.calls)


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


def test_target_probe_is_diagnostic_without_verified_https(monkeypatch) -> None:
    monkeypatch.setattr("socket.create_connection", lambda *args, **kwargs: SocketConnection())
    monkeypatch.setattr(
        ConnectivityProbe, "verified_https_available", lambda self, interface: False
    )
    adapter = NetworkManagerAdapter(
        WifiConfig(), AccessPointConfig(), HealthConfig(), runner=HealthRunner(False)
    )

    snapshot = adapter.snapshot()

    assert not snapshot.dns_reachable
    assert snapshot.target_reachable
    assert snapshot.health_level == "local"
    assert not snapshot.uplink_ready
    assert snapshot.error == "https_verification_failed"


def test_dns_alone_cannot_validate_a_captive_network(monkeypatch) -> None:
    def unreachable(*args, **kwargs):
        raise OSError("blocked target")

    monkeypatch.setattr("socket.create_connection", unreachable)
    monkeypatch.setattr(
        ConnectivityProbe, "verified_https_available", lambda self, interface: False
    )
    adapter = NetworkManagerAdapter(
        WifiConfig(), AccessPointConfig(), HealthConfig(), runner=HealthRunner(True)
    )

    snapshot = adapter.snapshot()

    assert snapshot.dns_reachable
    assert not snapshot.target_reachable
    assert not snapshot.uplink_ready
    assert snapshot.health_level == "local"
    assert snapshot.error == "https_verification_failed"


def test_strict_health_policy_can_require_non_dns_internet_evidence(monkeypatch) -> None:
    def unreachable(*args, **kwargs):
        raise OSError("blocked target")

    monkeypatch.setattr("socket.create_connection", unreachable)
    monkeypatch.setattr(
        ConnectivityProbe, "verified_https_available", lambda self, interface: False
    )
    health = HealthConfig(accept_dns_as_internet_evidence=False)
    adapter = NetworkManagerAdapter(
        WifiConfig(), AccessPointConfig(), health, runner=HealthRunner(True)
    )

    snapshot = adapter.snapshot()

    assert not snapshot.uplink_ready
    assert snapshot.health_level == "local"
    assert snapshot.error == "https_verification_failed"


def test_verified_https_on_wifi_validates_when_global_nm_is_unknown(monkeypatch) -> None:
    monkeypatch.setattr("socket.create_connection", lambda *args, **kwargs: SocketConnection())
    monkeypatch.setattr(
        ConnectivityProbe, "verified_https_available", lambda self, interface: interface == "wlan0"
    )
    snapshot = NetworkManagerAdapter(
        WifiConfig(), AccessPointConfig(), HealthConfig(), runner=HealthRunner(False)
    ).snapshot()
    assert snapshot.https_reachable
    assert snapshot.uplink_ready
    assert snapshot.health_level == "internet"


class CaptivePortalRunner(HealthRunner):
    def run(self, args, timeout: float = 10.0) -> CommandResult:
        if tuple(args)[:3] == ("nmcli", "networking", "connectivity"):
            return CommandResult(0, "full\n")  # Ethernet/VPN may make global state full.
        if tuple(args)[:3] == ("nmcli", "-g", "GENERAL.IP4-CONNECTIVITY"):
            return CommandResult(0, "2 (portal)\n")
        return super().run(args, timeout)


class GlobalFullRunner(HealthRunner):
    def run(self, args, timeout: float = 10.0) -> CommandResult:
        if tuple(args)[:3] == ("nmcli", "networking", "connectivity"):
            return CommandResult(0, "full\n")
        return super().run(args, timeout)


def test_global_full_from_other_interface_does_not_validate_wifi(monkeypatch) -> None:
    monkeypatch.setattr("socket.create_connection", lambda *args, **kwargs: SocketConnection())
    monkeypatch.setattr(
        ConnectivityProbe, "verified_https_available", lambda self, interface: False
    )
    monkeypatch.setattr(
        ConnectivityProbe, "captive_portal_detected", lambda self, interface: False
    )
    snapshot = NetworkManagerAdapter(
        WifiConfig(), AccessPointConfig(), HealthConfig(), runner=GlobalFullRunner(True)
    ).snapshot()
    assert snapshot.connectivity_state == "full"
    assert not snapshot.uplink_ready
    assert snapshot.error == "https_verification_failed"


def test_device_portal_overrides_global_full_and_tcp_dns(monkeypatch) -> None:
    monkeypatch.setattr("socket.create_connection", lambda *args, **kwargs: SocketConnection())
    monkeypatch.setattr(
        ConnectivityProbe, "verified_https_available", lambda self, interface: False
    )
    snapshot = NetworkManagerAdapter(
        WifiConfig(), AccessPointConfig(), HealthConfig(), runner=CaptivePortalRunner(True)
    ).snapshot()
    assert snapshot.dns_reachable and snapshot.target_reachable
    assert snapshot.captive_portal
    assert not snapshot.uplink_ready
    assert snapshot.error == "captive_portal"


def test_http_login_redirect_is_portal_when_nm_checks_are_disabled(monkeypatch) -> None:
    monkeypatch.setattr("socket.create_connection", lambda *args, **kwargs: SocketConnection())
    monkeypatch.setattr(
        ConnectivityProbe, "verified_https_available", lambda self, interface: False
    )
    monkeypatch.setattr(
        ConnectivityProbe, "captive_portal_detected", lambda self, interface: interface == "wlan0"
    )
    snapshot = NetworkManagerAdapter(
        WifiConfig(), AccessPointConfig(), HealthConfig(), runner=HealthRunner(True)
    ).snapshot()
    assert snapshot.captive_portal
    assert snapshot.health_level == "portal"
    assert snapshot.error == "captive_portal"
    assert not snapshot.uplink_ready


def test_http_portal_probe_recognizes_login_redirect_but_not_expected_204(monkeypatch) -> None:
    import socket

    bound: list[object] = []

    class FakeSocket:
        def __init__(self, response):
            self.response = response

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def settimeout(self, timeout):
            return None

        def setsockopt(self, level, option, value):
            bound.append(value)

        def connect(self, endpoint):
            bound.append(endpoint)

        def sendall(self, request):
            bound.append(request)

        def recv(self, size):
            return self.response

    monkeypatch.setattr(socket, "SO_BINDTODEVICE", 25, raising=False)
    probe = ConnectivityProbe(
        HealthConfig(), ProbeRunner(CommandResult(0, "203.0.113.8 STREAM portal-probe\n"))
    )
    monkeypatch.setattr(socket, "socket", lambda *args: FakeSocket(b"HTTP/1.1 302 Found\r\n"))
    assert probe.captive_portal_detected("wlan0")
    assert b"wlan0\0" in bound
    assert ("203.0.113.8", 80) in bound
    monkeypatch.setattr(socket, "socket", lambda *args: FakeSocket(b"HTTP/1.1 204 No Content\r\n"))
    assert not probe.captive_portal_detected("wlan0")


def test_https_probe_binds_wifi_and_validates_tls(monkeypatch) -> None:
    import socket
    import ssl

    calls: list[object] = []

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def settimeout(self, timeout):
            calls.append(("timeout", timeout))

        def setsockopt(self, level, option, value):
            calls.append(("bind", level, option, value))

        def connect(self, endpoint):
            calls.append(("connect", endpoint))

    class FakeTls(FakeSocket):
        def sendall(self, request):
            calls.append(("request", request))

        def recv(self, size):
            return b"HTTP/1.1"

    class FakeContext:
        def wrap_socket(self, transport, server_hostname):
            calls.append(("tls", server_hostname))
            return FakeTls()

    monkeypatch.setattr(socket, "SO_BINDTODEVICE", 25, raising=False)
    monkeypatch.setattr(socket, "socket", lambda *args: FakeSocket())
    monkeypatch.setattr(ssl, "create_default_context", lambda: FakeContext())
    probe = ConnectivityProbe(
        HealthConfig(https_probe_hosts=("www.aliyun.com",)),
        ProbeRunner(CommandResult(0, "203.0.113.8 STREAM www.aliyun.com\n")),
    )
    assert probe.verified_https_available("wlan0")
    assert ("bind", socket.SOL_SOCKET, 25, b"wlan0\0") in calls
    assert ("connect", ("203.0.113.8", 443)) in calls
    assert ("tls", "www.aliyun.com") in calls


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


class ProfileLifecycleRunner(RecordingRunner):
    def __init__(self) -> None:
        super().__init__()
        self.profiles = {
            "broken-client": ("edgelink-provisioning", "wlan0", "infrastructure"),
            "other-radio": ("edgelink-provisioning", "wlan1", "ap"),
            "daemon": ("Hotspot", "wlan0", "ap"),
        }
        self.active_uuid = ""

    def run(self, args, timeout: float = 10.0) -> CommandResult:
        call = tuple(args)
        self.calls.append(call)
        if call[:4] == ("nmcli", "-t", "-f", "UUID,TYPE,NAME"):
            return CommandResult(0, "".join(
                f"{uid}:802-11-wireless:{values[0]}\n"
                for uid, values in self.profiles.items()
            ))
        if "connection.id,connection.interface-name,802-11-wireless.mode" in call:
            return CommandResult(0, "\n".join(self.profiles[call[-1]]) + "\n")
        if call[:3] == ("nmcli", "connection", "add"):
            uid = call[call.index("connection.uuid") + 1]
            self.profiles[uid] = (
                call[call.index("con-name") + 1],
                call[call.index("ifname") + 1],
                call[call.index("802-11-wireless.mode") + 1],
            )
        if call[:2] == ("nmcli", "-f"):
            return CommandResult(0, f"GENERAL.CON-UUID: {self.active_uuid}\n")
        return CommandResult(0, "")


def test_repeated_ap_restore_creates_once_and_uses_exact_uuid() -> None:
    runner = ProfileLifecycleRunner()
    adapter = NetworkManagerAdapter(
        WifiConfig(), AccessPointConfig(reserved_connection_names=("Hotspot",)),
        HealthConfig(), runner=runner,
    )
    assert adapter.start_ap("Zebao-1", "test-password")
    assert adapter.start_ap("Zebao-1", "test-password")
    additions = [call for call in runner.calls if call[:3] == ("nmcli", "connection", "add")]
    assert len(additions) == 1
    created_uuid = additions[0][additions[0].index("connection.uuid") + 1]
    assert additions[0][-2:] == ("connection.autoconnect", "no")
    modifies = [call for call in runner.calls if call[:3] == ("nmcli", "connection", "modify")]
    assert all(call[3] == created_uuid for call in modifies)
    assert not any(call[3] in {"broken-client", "other-radio", "daemon"} for call in modifies)
    assert "test-password" not in " ".join(part for call in runner.calls for part in call)
    assert adapter.stop_ap()
    assert runner.calls[-1] == ("nmcli", "connection", "down", "uuid", created_uuid)


def test_repeated_wifi_provisioning_reuses_uuid_without_duplicate_profiles() -> None:
    runner = ProfileLifecycleRunner()
    adapter = NetworkManagerAdapter(WifiConfig(), AccessPointConfig(), HealthConfig(), runner)
    assert adapter.connect("My WiFi", "test-password")
    assert adapter.connect("My WiFi", "test-password")
    additions = [call for call in runner.calls if call[:3] == ("nmcli", "connection", "add")]
    assert len(additions) == 1
    uid = additions[0][additions[0].index("connection.uuid") + 1]
    assert runner.profiles[uid] == (_profile_name("My WiFi"), "wlan0", "infrastructure")


def test_unreadable_profile_list_cannot_create_duplicates_or_claim_ap_stopped() -> None:
    runner = ProbeRunner(CommandResult(10, "", "NetworkManager unavailable"))
    adapter = NetworkManagerAdapter(WifiConfig(), AccessPointConfig(), HealthConfig(), runner)
    assert not adapter.start_ap("Zebao-1", "test-password")
    assert not adapter.connect("My WiFi", "test-password")
    assert not adapter.stop_ap()
    assert not any("add" in call or "modify" in call for call, _ in runner.calls)


def test_infrastructure_profile_with_ap_name_is_not_an_active_hotspot(monkeypatch) -> None:
    class WrongModeRunner(HealthRunner):
        def run(self, args, timeout: float = 10.0) -> CommandResult:
            result = super().run(args, timeout)
            return CommandResult(
                result.returncode, result.stdout.replace("Office", "edgelink-provisioning")
            )

    monkeypatch.setattr(ConnectivityProbe, "verified_https_available", lambda *args: True)
    snapshot = NetworkManagerAdapter(
        WifiConfig(), AccessPointConfig(), HealthConfig(), WrongModeRunner(False)
    ).snapshot()
    assert not snapshot.ap_active
    assert snapshot.uplink_ready
