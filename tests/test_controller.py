from __future__ import annotations

import time
from dataclasses import replace
from threading import Event, Thread

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
        self.restore_result = False
        self.restored_snapshot: NetworkSnapshot | None = None
        self.commit_result = True
        self.reconnect_result = False

    def snapshot(self) -> NetworkSnapshot:
        return self.current

    def scan(self) -> list[WifiNetwork]:
        return []

    def reconnect_saved(self) -> bool:
        self.actions.append("reconnect")
        return self.reconnect_result

    def connect(self, ssid: str, password: str) -> bool:
        self.actions.append(f"connect:{ssid}")
        if self.connect_result and self.connected_snapshot is not None:
            self.current = self.connected_snapshot
        return self.connect_result

    def commit(self, ssid: str) -> bool:
        self.actions.append(f"commit:{ssid}")
        return self.commit_result

    def forget(self, connection_name: str) -> bool:
        self.actions.append(f"forget:{connection_name}")
        return True

    def suspend(self, ssid: str) -> bool:
        self.actions.append(f"suspend:{ssid}")
        return True

    def restore(self, connection_name: str) -> bool:
        self.actions.append(f"restore:{connection_name}")
        if self.restore_result and self.restored_snapshot is not None:
            self.current = self.restored_snapshot
        return self.restore_result

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


class BlockingNetwork(FakeNetwork):
    def __init__(self, snapshot: NetworkSnapshot) -> None:
        super().__init__(snapshot)
        self.connect_started = Event()
        self.release_connect = Event()

    def connect(self, ssid: str, password: str) -> bool:
        self.actions.append(f"connect:{ssid}")
        self.connect_started.set()
        self.release_connect.wait(timeout=2)
        return False


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
        connection_name="Office",
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
            reconnect_settle_seconds=4,
            reconnect_attempts_before_ap=2,
            ap_fallback_seconds=10,
            recovery_cooldown_seconds=5,
            provisioning_start_delay_seconds=1,
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


def test_successful_reconnect_gets_a_settle_window_before_ap_fallback(monkeypatch) -> None:
    monkeypatch.setenv("EDGELINK_AP_PASSWORD", "safe-password")
    clock = FakeClock()
    network = FakeNetwork(offline_network())
    network.reconnect_result = True
    controller = EdgeLinkController(config(), network, FakeOverlay(OverlaySnapshot()), clock)

    controller.tick()
    clock.advance(3)
    status = controller.tick()

    assert status.reason == "uplink_settling"
    assert network.actions == ["reconnect"]
    assert not any(action.startswith("start_ap:") for action in network.actions)

    clock.advance(0.9)
    assert controller.tick().reason == "uplink_settling"


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
    assert network.actions == ["stop_ap", "connect:New WiFi", "commit:New WiFi"]


def test_async_provisioning_acknowledges_before_network_switch(monkeypatch) -> None:
    monkeypatch.setenv("EDGELINK_AP_PASSWORD", "safe-password")
    clock = FakeClock()
    network = FakeNetwork(replace(offline_network(), ap_active=True))
    controller = EdgeLinkController(config(), network, FakeOverlay(OverlaySnapshot()), clock)
    controller.tick()

    accepted, reason, operation_id = controller.request_wifi_provisioning(
        "New WiFi", "12345678"
    )

    assert accepted
    assert reason == "accepted"
    assert operation_id
    assert network.actions == []
    assert controller.status().provisioning.phase == "queued"

    clock.advance(0.9)
    controller.tick()
    assert network.actions == []

    clock.advance(0.1)
    controller.tick()
    assert network.actions[:2] == ["stop_ap", "connect:New WiFi"]
    assert controller.status().provisioning.phase == "failed"


def test_latest_queued_provisioning_request_replaces_stale_request(monkeypatch) -> None:
    monkeypatch.setenv("EDGELINK_AP_PASSWORD", "safe-password")
    clock = FakeClock()
    network = FakeNetwork(replace(offline_network(), ap_active=True))
    controller = EdgeLinkController(config(), network, FakeOverlay(OverlaySnapshot()), clock)
    controller.tick()

    first = controller.request_wifi_provisioning("Old WiFi", "12345678")
    second = controller.request_wifi_provisioning("New WiFi", "abcdefgh")
    clock.advance(1)
    controller.tick()

    assert first[0] and second[0]
    assert second[1] == "queued_request_replaced"
    assert "connect:Old WiFi" not in network.actions
    assert "connect:New WiFi" in network.actions
    assert controller.status().counters.provisioning_replacements == 1


def test_running_provisioning_rejects_new_request_and_keeps_status_responsive(
    monkeypatch,
) -> None:
    monkeypatch.setenv("EDGELINK_AP_PASSWORD", "safe-password")
    clock = FakeClock()
    network = BlockingNetwork(replace(offline_network(), ap_active=True))
    controller = EdgeLinkController(config(), network, FakeOverlay(OverlaySnapshot()), clock)
    controller.tick()
    controller.request_wifi_provisioning("First WiFi", "12345678")
    clock.advance(1)
    worker = Thread(target=controller.tick)
    worker.start()
    assert network.connect_started.wait(timeout=1)

    started = time.monotonic()
    accepted, reason, operation_id = controller.request_wifi_provisioning(
        "Second WiFi", "abcdefgh"
    )
    status = controller.status()
    elapsed = time.monotonic() - started

    assert not accepted
    assert reason == "provisioning_busy"
    assert operation_id == status.provisioning.operation_id
    assert status.provisioning.phase == "running"
    assert elapsed < 0.2

    network.release_connect.set()
    worker.join(timeout=2)
    assert not worker.is_alive()


def test_failed_remote_switch_restores_previous_healthy_network(monkeypatch) -> None:
    monkeypatch.setenv("EDGELINK_AP_PASSWORD", "safe-password")
    network = FakeNetwork(healthy_network())
    network.restore_result = True
    network.restored_snapshot = healthy_network()
    controller = EdgeLinkController(config(), network, FakeOverlay(OverlaySnapshot()), FakeClock())

    ok, reason = controller.provision_wifi("Bad WiFi", "12345678")

    assert not ok
    assert reason == "connection_failed_previous_restored"
    assert network.actions == [
        "connect:Bad WiFi",
        "suspend:Bad WiFi",
        "restore:Office",
    ]
    assert not any(action.startswith("start_ap:") for action in network.actions)
    assert controller.status().counters.previous_network_restores == 1


def test_uncommitted_verified_profile_rolls_back_instead_of_surviving_reboot(
    monkeypatch,
) -> None:
    monkeypatch.setenv("EDGELINK_AP_PASSWORD", "safe-password")
    network = FakeNetwork(healthy_network())
    network.connect_result = True
    network.connected_snapshot = replace(healthy_network(), connection_name="candidate")
    network.commit_result = False
    network.restore_result = True
    network.restored_snapshot = healthy_network()
    controller = EdgeLinkController(config(), network, FakeOverlay(OverlaySnapshot()), FakeClock())

    ok, reason = controller.provision_wifi("Candidate WiFi", "12345678")

    assert not ok
    assert reason == "profile_commit_failed_previous_restored"
    assert network.actions == [
        "connect:Candidate WiFi",
        "commit:Candidate WiFi",
        "suspend:Candidate WiFi",
        "restore:Office",
    ]


def test_wifi_credentials_follow_byte_limits_and_reject_secret_file_injection() -> None:
    controller = EdgeLinkController(
        config(), FakeNetwork(offline_network()), FakeOverlay(OverlaySnapshot()), FakeClock()
    )

    assert controller.provision_wifi("中" * 11, "12345678") == (False, "invalid_ssid")
    assert controller.provision_wifi("Wifi", "1234567\nnext=value") == (
        False,
        "invalid_wifi_password",
    )
    accepted, reason, _ = controller.request_wifi_provisioning("Wifi", "a" * 64)
    assert accepted and reason == "accepted"


def test_status_is_a_deep_snapshot() -> None:
    controller = EdgeLinkController(
        config(), FakeNetwork(healthy_network()), FakeOverlay(OverlaySnapshot()), FakeClock()
    )
    snapshot = controller.status()
    snapshot.counters.ticks = 999
    snapshot.provisioning.phase = "corrupted"

    current = controller.status()
    assert current.counters.ticks == 0
    assert current.provisioning.phase == "idle"


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
