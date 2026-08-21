from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import replace

from .config import AgentConfig
from .models import AgentState, AgentStatus, NetworkSnapshot
from .ports import Clock, NetworkPort, OverlayPort

LOGGER = logging.getLogger("edgelink.controller")
_SAFE_SSID = re.compile(r"^[^\x00-\x1f\x7f]{1,32}$")


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class EdgeLinkController:
    """Single-writer network recovery state machine.

    NetworkManager events only wake this controller. They never execute recovery
    themselves, preventing stale dispatcher events from racing newer link state.
    """

    def __init__(
        self,
        config: AgentConfig,
        network: NetworkPort,
        overlay: OverlayPort,
        clock: Clock | None = None,
    ) -> None:
        self.config = config
        self._network = network
        self._overlay = overlay
        self._clock = clock or SystemClock()
        now = self._clock.monotonic()
        self._status = AgentStatus(state_changed_at=self._clock.time())
        self._boot_at = now
        self._bad_since: float | None = None
        self._good_since: float | None = None
        self._overlay_bad_since: float | None = None
        self._last_reconnect_at = float("-inf")
        self._last_ap_action_at = float("-inf")
        self._last_overlay_action_at = float("-inf")
        self._last_provisioning_activity = float("-inf")
        self._ap_observed = False
        self._reconnects_in_outage = 0
        self._lock = threading.RLock()
        self._event_lock = threading.Lock()
        self._pending_event_at = 0.0
        self._wake = threading.Event()
        self._stop = threading.Event()

    def status(self) -> AgentStatus:
        with self._lock:
            return replace(self._status)

    def notify_network_event(self) -> None:
        with self._event_lock:
            self._pending_event_at = self._clock.time()
        self._wake.set()

    def note_provisioning_activity(self) -> None:
        with self._lock:
            self._last_provisioning_activity = self._clock.monotonic()

    def run(self) -> None:
        LOGGER.info("EdgeLink controller started for %s", self.config.device_name)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                LOGGER.exception("controller tick failed")
                self._transition(AgentState.DEGRADED, "controller_error")
            self._wake.wait(self.config.tick_seconds)
            self._wake.clear()
        self._transition(AgentState.STOPPED, "service_stopped")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def tick(self) -> AgentStatus:
        with self._lock:
            with self._event_lock:
                if self._pending_event_at > self._status.last_event_at:
                    self._status.last_event_at = self._pending_event_at
            now = self._clock.monotonic()
            network = self._network.snapshot()
            overlay = self._overlay.snapshot()
            self._status.network = network
            self._status.overlay = overlay
            self._status.counters.ticks += 1
            if network.uplink_ready and not network.ap_active:
                self._handle_healthy_uplink(now, overlay.running, overlay.connected)
            else:
                self._handle_unhealthy_uplink(now, network)
            return replace(self._status)

    def _handle_healthy_uplink(
        self, now: float, overlay_running: bool, overlay_ready: bool
    ) -> None:
        if self._good_since is None:
            self._good_since = now
        self._bad_since = None
        self._reconnects_in_outage = 0

        if self.config.overlay.enabled:
            if not overlay_running:
                if now - self._last_overlay_action_at >= 5.0:
                    self._last_overlay_action_at = now
                    self._status.counters.overlay_starts += 1
                    self._overlay.start()
                self._transition(AgentState.DEGRADED, "overlay_starting")
                return
            if not overlay_ready:
                if self._overlay_bad_since is None:
                    self._overlay_bad_since = now
                elapsed = now - self._overlay_bad_since
                if (
                    elapsed >= self.config.overlay.disconnected_restart_seconds
                    and now - self._last_overlay_action_at
                    >= self.config.overlay.disconnected_restart_seconds
                ):
                    self._last_overlay_action_at = now
                    self._status.counters.overlay_restarts += 1
                    self._overlay.restart()
                self._transition(AgentState.DEGRADED, "overlay_disconnected")
                return
        self._overlay_bad_since = None
        self._transition(AgentState.ONLINE, "uplink_and_overlay_ready")

    def _handle_unhealthy_uplink(self, now: float, network: NetworkSnapshot) -> None:
        self._good_since = None
        self._overlay_bad_since = None
        self._status.counters.uplink_failures += 1
        if self._bad_since is None:
            self._bad_since = now
        outage_seconds = now - self._bad_since

        if network.ap_active:
            if not self._ap_observed:
                self._ap_observed = True
                self._last_provisioning_activity = now
            quiet_seconds = now - self._last_provisioning_activity
            retry_due = (
                self.config.wifi.auto_retry_saved_profiles
                and quiet_seconds
                >= max(
                    self.config.wifi.ap_client_hold_seconds,
                    self.config.wifi.ap_saved_retry_seconds,
                )
                and now - self._last_reconnect_at >= self.config.wifi.ap_saved_retry_seconds
            )
            if retry_due:
                self._last_reconnect_at = now
                self._status.counters.reconnect_attempts += 1
                self._network.stop_ap()
                if self._network.reconnect_saved():
                    self._transition(AgentState.RECOVERING, "retrying_saved_wifi_from_ap")
                    return
                self._start_ap_locked()
            self._transition(AgentState.PROVISIONING, "fallback_ap_active")
            wait_seconds = max(
                self.config.wifi.ap_client_hold_seconds - quiet_seconds,
                self.config.wifi.ap_saved_retry_seconds - (now - self._last_reconnect_at),
                0.0,
            )
            self._status.next_action_at = self._clock.time() + wait_seconds
            return
        self._ap_observed = False

        grace = max(
            self.config.wifi.startup_grace_seconds
            if now - self._boot_at < self.config.wifi.startup_grace_seconds
            else 0.0,
            self.config.wifi.ap_fallback_seconds,
        )
        can_reconnect = (
            self.config.wifi.auto_retry_saved_profiles
            and self._reconnects_in_outage < self.config.wifi.reconnect_attempts_before_ap
            and now - self._last_reconnect_at >= self.config.wifi.reconnect_interval_seconds
        )
        if can_reconnect:
            self._last_reconnect_at = now
            self._reconnects_in_outage += 1
            self._status.counters.reconnect_attempts += 1
            self._transition(AgentState.RECOVERING, "retrying_saved_wifi")
            self._network.reconnect_saved()
            return

        if (
            outage_seconds >= grace
            and self.config.access_point.enabled
            and now - self._last_ap_action_at >= self.config.wifi.recovery_cooldown_seconds
            and self._start_ap_locked()
        ):
            self._transition(AgentState.PROVISIONING, "fallback_ap_started")
            return
        if outage_seconds >= grace and self.config.access_point.enabled:
            self._transition(AgentState.DEGRADED, "fallback_ap_failed")
            return

        self._status.next_action_at = self._clock.time() + max(0.0, grace - outage_seconds)
        self._transition(AgentState.RECOVERING, _network_reason(network))

    def provision_wifi(self, ssid: str, password: str) -> tuple[bool, str]:
        if not _SAFE_SSID.fullmatch(ssid):
            return False, "invalid_ssid"
        if password and not 8 <= len(password) <= 63:
            return False, "invalid_wifi_password"
        with self._lock:
            was_ap = self._status.network.ap_active
            if was_ap:
                self._network.stop_ap()
            connected = self._network.connect(ssid, password)
            verified = self._network.snapshot() if connected else None
            if connected and verified is not None and verified.uplink_ready:
                self._status.counters.provisioning_successes += 1
                self._bad_since = None
                self._good_since = self._clock.monotonic()
                self._wake.set()
                return True, "connected"
            self._status.counters.provisioning_failures += 1
            if was_ap or self.config.access_point.enabled:
                self._start_ap_locked()
            return False, "connection_not_usable" if connected else "connection_failed"

    def start_ap(self) -> bool:
        with self._lock:
            return self._start_ap_locked()

    def _start_ap_locked(self) -> bool:
        password = os.getenv(self.config.access_point.password_env, "")
        if len(password) < 8:
            LOGGER.error("AP password is not configured")
            return False
        self._last_ap_action_at = self._clock.monotonic()
        ssid = f"{self.config.access_point.ssid_prefix}-{_device_suffix(self.config.device_name)}"
        result = self._network.start_ap(ssid, password)
        if result:
            self._status.counters.ap_starts += 1
            self._ap_observed = True
            self._last_provisioning_activity = self._clock.monotonic()
        return result

    def stop_ap(self) -> bool:
        with self._lock:
            self._last_ap_action_at = self._clock.monotonic()
            result = self._network.stop_ap()
            if result:
                self._status.counters.ap_stops += 1
            self._wake.set()
            return result

    def recover_now(self) -> bool:
        with self._lock:
            self._last_reconnect_at = self._clock.monotonic()
            self._status.counters.reconnect_attempts += 1
            result = self._network.reconnect_saved()
            self._wake.set()
            return result

    def _transition(self, state: AgentState, reason: str) -> None:
        if self._status.state != state or self._status.reason != reason:
            LOGGER.info("state=%s reason=%s", state.value, reason)
            self._status.state = state
            self._status.reason = reason
            self._status.state_changed_at = self._clock.time()
        if state in {AgentState.ONLINE, AgentState.PROVISIONING}:
            self._status.next_action_at = None


def _device_suffix(device_name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9-]", "-", device_name).strip("-").lower()
    return safe[-16:] or "device"


def _network_reason(snapshot: NetworkSnapshot) -> str:
    if snapshot.error == "network_manager_unavailable":
        return snapshot.error
    if not snapshot.link_up:
        return "wifi_disconnected"
    if not snapshot.ipv4:
        return "dhcp_incomplete"
    if not snapshot.gateway:
        return "default_route_missing"
    if not snapshot.dns_servers or snapshot.error == "dns_unavailable":
        return "dns_unavailable"
    if snapshot.captive_portal:
        return "captive_portal"
    return "uplink_target_unreachable"
