from __future__ import annotations

from dataclasses import replace

from edgelink_agent.config import (
    AccessPointConfig,
    AgentConfig,
    OverlayConfig,
    WifiConfig,
)
from edgelink_agent.controller import EdgeLinkController
from edgelink_agent.models import AgentState, NetworkSnapshot, OverlaySnapshot, WifiNetwork


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return 1_800_000_000.0 + self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeNetwork:
    def __init__(self, snapshot: NetworkSnapshot) -> None:
        self.current = snapshot
        self.actions: list[str] = []
        self.connect_result = False
        self.connected_snapshot: NetworkSnapshot | None = None
        self.start_ap_results: list[bool] = []

    def snapshot(self) -> NetworkSnapshot:
        return self.current

    def scan(self) -> list[WifiNetwork]:
        return []

    def reconnect_saved(self) -> bool:
        self.actions.append("reconnect")
        return False

    def connect(self, ssid: str, password: str) -> bool:
        self.actions.append(f"connect:{ssid}")
        if self.connect_result and self.connected_snapshot is not None:
            self.current = self.connected_snapshot
        return self.connect_result

    def forget(self, connection_name: str) -> bool:
        self.actions.append(f"forget:{connection_name}")
        return True

    def suspend(self, ssid: str) -> bool:
        self.actions.append(f"suspend:{ssid}")
        return True

    def start_ap(self, ssid: str, password: str) -> bool:
        self.actions.append(f"start_ap:{ssid}")
        result = self.start_ap_results.pop(0) if self.start_ap_results else True
        if not result:
            return False
        self.current = replace(
            self.current,
            ap_active=True,
            connection_name="edgelink-provisioning",
            ssid=ssid,
        )
        return True

    def stop_ap(self) -> bool:
        self.actions.append("stop_ap")
        self.current = replace(self.current, ap_active=False, connection_name="")
        return True


class FakeOverlay:
    def __init__(self, snapshot: OverlaySnapshot) -> None:
        self.current = snapshot
        self.actions: list[str] = []

    def snapshot(self) -> OverlaySnapshot:
        return self.current

    def start(self) -> bool:
        self.actions.append("start")
        return True

    def restart(self) -> bool:
        self.actions.append("restart")
        return True


def healthy_network() -> NetworkSnapshot:
    return NetworkSnapshot(
        interface="wlan0",
        link_up=True,
        ssid="Office",
        ipv4="192.168.1.20/24",
        gateway="192.168.1.1",
        dns_servers=("192.168.1.1",),
        target_reachable=True,
    )


def offline_network() -> NetworkSnapshot:
    return NetworkSnapshot(interface="wlan0")


def config() -> AgentConfig:
    return AgentConfig(
        device_name="zebao-0",
        wifi=WifiConfig(
            startup_grace_seconds=10,
            reconnect_interval_seconds=2,
            reconnect_attempts_before_ap=2,
            ap_fallback_seconds=10,
            recovery_cooldown_seconds=5,
            ap_saved_retry_seconds=10,
            ap_client_hold_seconds=5,
        ),
        access_point=AccessPointConfig(enabled=True),
        overlay=OverlayConfig(enabled=True, disconnected_restart_seconds=5),
    )


def test_healthy_uplink_and_overlay_become_online(monkeypatch) -> None:
    monkeypatch.setenv("EDGELINK_AP_PASSWORD", "safe-password")
    network = FakeNetwork(healthy_network())
    overlay = FakeOverlay(OverlaySnapshot(running=True, connected=True, virtual_ip="192.167.1.2"))
    controller = EdgeLinkController(config(), network, overlay, FakeClock())

    status = controller.tick()

    assert status.state is AgentState.ONLINE
    assert not network.actions
    assert not overlay.actions


def test_offline_retries_then_starts_fallback_ap(monkeypatch) -> None:
    monkeypatch.setenv("EDGELINK_AP_PASSWORD", "safe-password")
    clock = FakeClock()
    network = FakeNetwork(offline_network())
    overlay = FakeOverlay(OverlaySnapshot(running=False, connected=False))
    controller = EdgeLinkController(config(), network, overlay, clock)

    assert controller.tick().state is AgentState.RECOVERING
    clock.advance(3)
    assert controller.tick().state is AgentState.RECOVERING
    clock.advance(8)
    status = controller.tick()

    assert status.state is AgentState.PROVISIONING
    assert sum(action == "reconnect" for action in network.actions) == 2
    assert any(action.startswith("start_ap:EdgeLink-zebao-0") for action in network.actions)


def test_product_template_uses_exact_ssid_without_repeated_device_name(monkeypatch) -> None:
    monkeypatch.setenv("EDGELINK_AP_PASSWORD", "safe-password")
    network = FakeNetwork(offline_network())
    product = replace(
        config(),
        device_name="zebao-1",
        access_point=AccessPointConfig(
            enabled=True,
            ssid="Zebao-1",
            ssid_prefix="Zebao",
        ),
    )
    controller = EdgeLinkController(product, network, FakeOverlay(OverlaySnapshot()))

    assert controller.start_ap()
    assert network.actions == ["start_ap:Zebao-1"]


def test_foreign_legacy_ap_is_reconciled_to_product_configuration(monkeypatch) -> None:
    monkeypatch.setenv("EDGELINK_AP_PASSWORD", "safe-password")
    network = FakeNetwork(
        replace(
            offline_network(),
            ap_active=True,
            connection_name="Hotspot",
            ssid="reachy-mini-ap",
        )
    )
    product = replace(
        config(),
        device_name="zebao-1",
        access_point=AccessPointConfig(
            enabled=True,
            connection_name="Hotspot",
            ssid="Zebao-1",
            ssid_prefix="Zebao",
            reuse_existing_profile=True,
        ),
    )
    controller = EdgeLinkController(product, network, FakeOverlay(OverlaySnapshot()))

    status = controller.tick()

    assert status.state is AgentState.PROVISIONING
    assert status.reason == "fallback_ap_reconciled"
    assert status.counters.ap_reconciliations == 1
    assert network.actions == ["start_ap:Zebao-1"]
    assert network.current.ssid == "Zebao-1"


def test_overlay_failure_never_starts_ap(monkeypatch) -> None:
    monkeypatch.setenv("EDGELINK_AP_PASSWORD", "safe-password")
    clock = FakeClock()
    network = FakeNetwork(healthy_network())
    overlay = FakeOverlay(OverlaySnapshot(running=True, connected=False))
    controller = EdgeLinkController(config(), network, overlay, clock)

    assert controller.tick().reason == "overlay_disconnected"
    clock.advance(6)
    controller.tick()

    assert overlay.actions == ["restart"]
    assert not any(action.startswith("start_ap") for action in network.actions)


def test_failed_provisioning_restores_ap(monkeypatch) -> None:
    monkeypatch.setenv("EDGELINK_AP_PASSWORD", "safe-password")
    network = FakeNetwork(replace(offline_network(), ap_active=True))
    overlay = FakeOverlay(OverlaySnapshot())
    controller = EdgeLinkController(config(), network, overlay, FakeClock())
    controller.tick()

    ok, reason = controller.provision_wifi("New WiFi", "12345678")

    assert not ok
    assert reason == "connection_failed"
    assert network.actions == [
        "stop_ap",
        "connect:New WiFi",
        "suspend:New WiFi",
        "start_ap:EdgeLink-zebao-0",
    ]


def test_failed_provisioning_retries_ap_restore_without_long_cooldown(monkeypatch) -> None:
    monkeypatch.setenv("EDGELINK_AP_PASSWORD", "safe-password")
    clock = FakeClock()
    network = FakeNetwork(replace(offline_network(), ap_active=True))
    network.start_ap_results = [False, True]
    controller = EdgeLinkController(config(), network, FakeOverlay(OverlaySnapshot()), clock)
    controller.tick()

    ok, reason = controller.provision_wifi("New WiFi", "12345678")

    assert not ok
    assert reason == "connection_failed_ap_restoring"
    assert "suspend:New WiFi" in network.actions
    assert controller.status().reason == "fallback_ap_restore_pending"
    assert controller.status().counters.ap_start_failures == 1

    clock.advance(2)
    assert controller.tick().reason == "fallback_ap_restore_pending"
    assert len([item for item in network.actions if item.startswith("start_ap:")]) == 1

    clock.advance(1)
    status = controller.tick()
    assert status.state is AgentState.PROVISIONING
    assert status.reason == "fallback_ap_restored"
    assert status.counters.ap_restore_retries == 1
    assert status.counters.ap_starts == 1
    assert network.current.ap_active


def test_normal_fallback_retries_failed_ap_start_quickly(monkeypatch) -> None:
    monkeypatch.setenv("EDGELINK_AP_PASSWORD", "safe-password")
    clock = FakeClock()
    network = FakeNetwork(offline_network())
    network.start_ap_results = [False, True]
    controller = EdgeLinkController(config(), network, FakeOverlay(OverlaySnapshot()), clock)

    controller.tick()
    clock.advance(3)
    controller.tick()
    clock.advance(8)
    pending = controller.tick()
    assert pending.reason == "fallback_ap_restore_pending"

    clock.advance(3)
    restored = controller.tick()
    assert restored.state is AgentState.PROVISIONING
    assert restored.reason == "fallback_ap_restored"
    assert restored.counters.ap_start_failures == 1
    assert restored.counters.ap_restore_retries == 1


def test_explicit_stop_cancels_pending_ap_restore(monkeypatch) -> None:
    monkeypatch.setenv("EDGELINK_AP_PASSWORD", "safe-password")
    clock = FakeClock()
    network = FakeNetwork(offline_network())
    network.start_ap_results = [False]
    controller = EdgeLinkController(config(), network, FakeOverlay(OverlaySnapshot()), clock)

    assert not controller.start_ap()
    assert controller.status().reason == "fallback_ap_restore_pending"
    assert controller.stop_ap()
    clock.advance(3)
    controller.tick()

    assert len([item for item in network.actions if item.startswith("start_ap:")]) == 1


def test_successful_provisioning_keeps_new_network(monkeypatch) -> None:
    monkeypatch.setenv("EDGELINK_AP_PASSWORD", "safe-password")
    network = FakeNetwork(replace(offline_network(), ap_active=True))
    network.connect_result = True
    network.connected_snapshot = healthy_network()
    overlay = FakeOverlay(OverlaySnapshot())
    controller = EdgeLinkController(config(), network, overlay, FakeClock())
    controller.tick()

    ok, reason = controller.provision_wifi("New WiFi", "12345678")

    assert ok
    assert reason == "connected"
    assert network.actions == ["stop_ap", "connect:New WiFi"]


def test_invalid_credentials_do_not_touch_network() -> None:
    network = FakeNetwork(offline_network())
    overlay = FakeOverlay(OverlaySnapshot())
    controller = EdgeLinkController(config(), network, overlay, FakeClock())

    assert controller.provision_wifi("", "12345678") == (False, "invalid_ssid")
    assert controller.provision_wifi("Wifi", "short") == (False, "invalid_wifi_password")
    assert network.actions == []


def test_active_provisioning_page_holds_ap_then_idle_retry_restores_it(monkeypatch) -> None:
    monkeypatch.setenv("EDGELINK_AP_PASSWORD", "safe-password")
    clock = FakeClock()
    network = FakeNetwork(replace(offline_network(), ap_active=True))
    overlay = FakeOverlay(OverlaySnapshot())
    controller = EdgeLinkController(config(), network, overlay, clock)

    controller.tick()
    controller.note_provisioning_activity()
    clock.advance(6)
    controller.tick()
    assert network.actions == []

    clock.advance(5)
    status = controller.tick()
    assert status.state is AgentState.PROVISIONING
    assert network.actions == ["stop_ap", "reconnect", "start_ap:EdgeLink-zebao-0"]


def test_network_event_timestamp_is_consumed_on_next_tick() -> None:
    clock = FakeClock()
    network = FakeNetwork(healthy_network())
    overlay = FakeOverlay(OverlaySnapshot(running=True, connected=True))
    controller = EdgeLinkController(config(), network, overlay, clock)

    controller.notify_network_event()
    assert controller.status().last_event_at == 0
    status = controller.tick()

    assert status.last_event_at == clock.time()
