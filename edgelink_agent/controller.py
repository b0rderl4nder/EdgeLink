from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass

from .config import AgentConfig, access_point_ssid
from .models import AgentState, AgentStatus, NetworkSnapshot
from .ports import Clock, NetworkPort, OverlayPort

LOGGER = logging.getLogger("edgelink.controller")
_SAFE_SSID = re.compile(r"^[^\x00-\x1f\x7f]{1,32}$")
_HEX_PSK = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_AP_PASSWORD = re.compile(r"^(?:[0-9a-fA-F]{64}|[A-Za-z0-9._~@%+=:-]{8,63})$")


@dataclass(frozen=True, slots=True)
class _ProvisioningCommand:
    operation_id: str
    ssid: str
    password: str
    requested_at: float
    not_before: float


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
        self._published_status = deepcopy(self._status)
        self._boot_at = now
        self._bad_since: float | None = None
        self._good_since: float | None = None
        self._overlay_bad_since: float | None = None
        self._last_reconnect_at = float("-inf")
        self._uplink_settle_until = float("-inf")
        self._last_ap_action_at = float("-inf")
        self._last_overlay_action_at = float("-inf")
        self._last_provisioning_activity = float("-inf")
        self._ap_observed = False
        self._ap_restore_pending = False
        self._reconnects_in_outage = 0
        self._lock = threading.RLock()
        self._status_view_lock = threading.Lock()
        self._command_lock = threading.Lock()
        self._pending_provisioning: _ProvisioningCommand | None = None
        self._active_provisioning_id = ""
        self._active_checkpoint = ""
        self._event_lock = threading.Lock()
        self._pending_event_at = 0.0
        self._wake = threading.Event()
        self._stop = threading.Event()

    def status(self) -> AgentStatus:
        with self._status_view_lock:
            return deepcopy(self._published_status)

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
                with self._lock:
                    self._transition(AgentState.DEGRADED, "controller_error")
                    self._publish_status_locked()
            self._wake.wait(self.config.tick_seconds)
            self._wake.clear()
        with self._lock:
            self._transition(AgentState.STOPPED, "service_stopped")
            self._publish_status_locked()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def tick(self) -> AgentStatus:
        with self._lock:
            with self._event_lock:
                if self._pending_event_at > self._status.last_event_at:
                    self._status.last_event_at = self._pending_event_at
            now = self._clock.monotonic()
            command = self._claim_pending_provisioning(now)
            if command is not None:
                self._execute_provisioning_locked(command)
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
            self._publish_status_locked()
            return deepcopy(self._status)

    def _handle_healthy_uplink(
        self, now: float, overlay_running: bool, overlay_ready: bool
    ) -> None:
        if self._good_since is None:
            self._good_since = now
        self._ap_restore_pending = False
        self._bad_since = None
        self._reconnects_in_outage = 0
        self._uplink_settle_until = float("-inf")

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

        # A product daemon may intentionally own an independent recovery AP on
        # the same radio.  Reserved profiles are a cooperative hand-off boundary:
        # while one is active EdgeLink keeps serving its provisioning API on that
        # subnet, but never renames, stops, or periodically displaces the AP.
        if (
            network.ap_active
            and network.connection_name
            in self.config.access_point.reserved_connection_names
        ):
            self._ap_restore_pending = False
            self._ap_observed = True
            self._transition(AgentState.PROVISIONING, "external_fallback_ap_active")
            return

        if self._ap_restore_pending:
            expected_ssid = access_point_ssid(self.config)
            if network.ap_active and network.ssid == expected_ssid:
                self._ap_restore_pending = False
            else:
                retry_seconds = self.config.wifi.ap_restore_retry_seconds
                wait_seconds = retry_seconds - (now - self._last_ap_action_at)
                if wait_seconds <= 0:
                    self._status.counters.ap_restore_retries += 1
                    if self._start_ap_locked():
                        self._ap_restore_pending = False
                        self._transition(AgentState.PROVISIONING, "fallback_ap_restored")
                        return
                    wait_seconds = retry_seconds
                self._status.next_action_at = self._clock.time() + max(0.0, wait_seconds)
                self._transition(AgentState.RECOVERING, "fallback_ap_restore_pending")
                return

        if network.ap_active:
            expected_ssid = access_point_ssid(self.config)
            if network.ssid and network.ssid != expected_ssid:
                if self._start_ap_locked():
                    self._status.counters.ap_reconciliations += 1
                    self._transition(AgentState.PROVISIONING, "fallback_ap_reconciled")
                else:
                    self._queue_ap_restore_locked()
                return
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
                    self._uplink_settle_until = now + self.config.wifi.reconnect_settle_seconds
                    self._transition(AgentState.RECOVERING, "retrying_saved_wifi_from_ap")
                    return
                if not self._start_ap_locked():
                    self._queue_ap_restore_locked()
                    return
            self._transition(AgentState.PROVISIONING, "fallback_ap_active")
            wait_seconds = max(
                self.config.wifi.ap_client_hold_seconds - quiet_seconds,
                self.config.wifi.ap_saved_retry_seconds - (now - self._last_reconnect_at),
                0.0,
            )
            self._status.next_action_at = self._clock.time() + wait_seconds
            return
        self._ap_observed = False

        if now < self._uplink_settle_until:
            self._status.next_action_at = self._clock.time() + (self._uplink_settle_until - now)
            self._transition(AgentState.RECOVERING, "uplink_settling")
            return

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
            if self._network.reconnect_saved():
                self._uplink_settle_until = now + self.config.wifi.reconnect_settle_seconds
            return

        if outage_seconds >= grace and self.config.access_point.enabled:
            if now - self._last_ap_action_at >= self.config.wifi.recovery_cooldown_seconds:
                if self._start_ap_locked():
                    self._transition(AgentState.PROVISIONING, "fallback_ap_started")
                else:
                    self._queue_ap_restore_locked()
                return
            self._status.next_action_at = self._clock.time() + max(
                0.0,
                self.config.wifi.recovery_cooldown_seconds - (now - self._last_ap_action_at),
            )
            self._transition(AgentState.DEGRADED, "fallback_ap_cooldown")
            return

        self._status.next_action_at = self._clock.time() + max(0.0, grace - outage_seconds)
        self._transition(AgentState.RECOVERING, _network_reason(network))

    def request_wifi_provisioning(self, ssid: str, password: str) -> tuple[bool, str, str]:
        validation_error = _validate_wifi_credentials(ssid, password)
        if validation_error:
            with self._lock:
                self._status.counters.provisioning_rejections += 1
                self._publish_status_locked()
            return False, validation_error, ""
        with self._command_lock:
            if self._active_provisioning_id:
                return False, "provisioning_busy", self._active_provisioning_id
        with self._lock, self._command_lock:
            if self._active_provisioning_id:
                return False, "provisioning_busy", self._active_provisioning_id
            now = self._clock.monotonic()
            command = _ProvisioningCommand(
                operation_id=uuid.uuid4().hex[:12],
                ssid=ssid,
                password=password,
                requested_at=self._clock.time(),
                not_before=now + self.config.wifi.provisioning_start_delay_seconds,
            )
            replaced = self._pending_provisioning is not None
            reason = "queued_request_replaced" if replaced else "accepted"
            self._pending_provisioning = command
            if replaced:
                self._status.counters.provisioning_replacements += 1
            self._set_provisioning_status(command, "queued", reason)
            self._publish_status_locked()
        self._wake.set()
        return True, reason, command.operation_id

    def provision_wifi(self, ssid: str, password: str) -> tuple[bool, str]:
        """Synchronous local entry point retained for CLI and deterministic tests."""
        validation_error = _validate_wifi_credentials(ssid, password)
        if validation_error:
            with self._lock:
                self._status.counters.provisioning_rejections += 1
                self._publish_status_locked()
            return False, validation_error
        command = _ProvisioningCommand(
            operation_id=uuid.uuid4().hex[:12],
            ssid=ssid,
            password=password,
            requested_at=self._clock.time(),
            not_before=self._clock.monotonic(),
        )
        with self._command_lock:
            if self._active_provisioning_id or self._pending_provisioning is not None:
                return False, "provisioning_busy"
        claimed = False
        try:
            with self._lock:
                with self._command_lock:
                    if self._active_provisioning_id or self._pending_provisioning is not None:
                        return False, "provisioning_busy"
                    self._active_provisioning_id = command.operation_id
                    claimed = True
                return self._execute_provisioning_locked(command)
        finally:
            if claimed:
                with self._command_lock:
                    if self._active_provisioning_id == command.operation_id:
                        self._active_provisioning_id = ""

    def _claim_pending_provisioning(self, now: float) -> _ProvisioningCommand | None:
        with self._command_lock:
            command = self._pending_provisioning
            if command is None or now < command.not_before:
                return None
            self._pending_provisioning = None
            self._active_provisioning_id = command.operation_id
            return command

    def _execute_provisioning_locked(self, command: _ProvisioningCommand) -> tuple[bool, str]:
        self._set_provisioning_status(command, "running", "connecting")
        self._publish_status_locked()
        try:
            result = self._provision_wifi_locked(command.ssid, command.password)
        except Exception:
            LOGGER.exception("unexpected provisioning failure")
            self._status.counters.provisioning_failures += 1
            if self._active_checkpoint:
                if self._network.rollback_checkpoint(self._active_checkpoint):
                    self._status.counters.checkpoints_rolled_back += 1
                else:
                    # Cancel the automatic timeout if explicit rollback itself failed;
                    # otherwise a later rollback could undo the AP/manual recovery.
                    self._network.commit_checkpoint(self._active_checkpoint)
                    self._status.counters.checkpoint_fallbacks += 1
                self._active_checkpoint = ""
            try:
                self._network.suspend(command.ssid)
            except Exception:
                LOGGER.exception("failed Wi-Fi profile suspension raised unexpectedly")
            try:
                ap_restored = self._start_ap_locked()
            except Exception:
                LOGGER.exception("AP restoration raised unexpectedly")
                self._status.counters.ap_start_failures += 1
                ap_restored = False
            if not ap_restored:
                self._queue_ap_restore_locked()
            result = False, "internal_error_ap_restoring"
        ok, reason = result
        self._set_provisioning_status(
            command,
            "succeeded" if ok else "failed",
            reason,
            finished=True,
        )
        self._publish_status_locked()
        with self._command_lock:
            self._active_provisioning_id = ""
        return result

    def _provision_wifi_locked(self, ssid: str, password: str) -> tuple[bool, str]:
        before = self._network.snapshot()
        was_ap = before.ap_active
        previous_connection = before.connection_name if before.uplink_ready else ""
        checkpoint = self._network.begin_checkpoint(
            self.config.wifi.checkpoint_timeout_seconds
        )
        self._active_checkpoint = checkpoint
        if checkpoint:
            self._status.counters.checkpoints_created += 1
        else:
            self._status.counters.checkpoint_fallbacks += 1
        if was_ap:
            self._network.stop_ap()
        attempt = self._network.connect(ssid, password)
        verified = self._network.snapshot() if attempt.ok else None
        if attempt.ok and verified is not None and verified.uplink_ready:
            if self._network.commit(ssid):
                if not checkpoint or self._network.commit_checkpoint(checkpoint):
                    if checkpoint:
                        self._status.counters.checkpoints_committed += 1
                    self._active_checkpoint = ""
                    self._status.counters.provisioning_successes += 1
                    self._ap_restore_pending = False
                    self._bad_since = None
                    self._good_since = self._clock.monotonic()
                    self._wake.set()
                    return True, "connected"
                failure_reason = "checkpoint_commit_failed"
            else:
                failure_reason = "profile_commit_failed"
        elif attempt.ok and verified is not None:
            failure_reason = _network_reason(verified)
        else:
            failure_reason = attempt.reason or "connection_failed"

        self._status.counters.provisioning_failures += 1
        if checkpoint and self._network.rollback_checkpoint(checkpoint):
            self._active_checkpoint = ""
            self._status.counters.checkpoints_rolled_back += 1
            restored = self._network.snapshot()
            if restored.uplink_ready or (was_ap and restored.ap_active):
                if restored.uplink_ready:
                    self._status.counters.previous_network_restores += 1
                    self._bad_since = None
                    self._good_since = self._clock.monotonic()
                self._wake.set()
                return False, f"{failure_reason}_checkpoint_rolled_back"
        elif checkpoint:
            self._network.commit_checkpoint(checkpoint)
            self._active_checkpoint = ""
            self._status.counters.checkpoint_fallbacks += 1

        # Old NetworkManager versions and failed D-Bus rollbacks retain the proven
        # explicit restore path. This also leaves an independent product daemon free
        # to act as a final recovery controller.
        if not self._network.suspend(ssid):
            LOGGER.warning("failed Wi-Fi profile could not be suspended")
        if previous_connection and self._network.restore(previous_connection):
            restored = self._network.snapshot()
            if restored.uplink_ready:
                self._status.counters.previous_network_restores += 1
                self._bad_since = None
                self._good_since = self._clock.monotonic()
                self._wake.set()
                return False, f"{failure_reason}_previous_restored"
        if was_ap or self.config.access_point.enabled:
            if not self._start_ap_locked():
                self._queue_ap_restore_locked()
                return False, f"{failure_reason}_ap_restoring"
            self._transition(AgentState.PROVISIONING, "fallback_ap_restored")
        return False, failure_reason

    def _set_provisioning_status(
        self,
        command: _ProvisioningCommand,
        phase: str,
        reason: str,
        *,
        finished: bool = False,
    ) -> None:
        status = self._status.provisioning
        status.operation_id = command.operation_id
        status.ssid = command.ssid
        status.phase = phase
        status.reason = reason
        status.requested_at = command.requested_at
        if phase == "running":
            status.started_at = self._clock.time()
            status.finished_at = 0.0
        if finished:
            status.finished_at = self._clock.time()

    def start_ap(self) -> bool:
        with self._lock:
            result = self._start_ap_locked()
            if result:
                self._ap_restore_pending = False
            else:
                self._queue_ap_restore_locked()
            self._publish_status_locked()
            return result

    def _start_ap_locked(self) -> bool:
        self._last_ap_action_at = self._clock.monotonic()
        password = os.getenv(self.config.access_point.password_env, "")
        if not _SAFE_AP_PASSWORD.fullmatch(password):
            LOGGER.error("AP password is missing or invalid")
            self._status.counters.ap_start_failures += 1
            return False
        ssid = access_point_ssid(self.config)
        result = self._network.start_ap(ssid, password)
        if result:
            self._status.counters.ap_starts += 1
            self._ap_observed = True
            self._last_provisioning_activity = self._clock.monotonic()
        else:
            self._status.counters.ap_start_failures += 1
        return result

    def _queue_ap_restore_locked(self) -> None:
        self._ap_restore_pending = True
        self._status.next_action_at = (
            self._clock.time() + self.config.wifi.ap_restore_retry_seconds
        )
        self._transition(AgentState.RECOVERING, "fallback_ap_restore_pending")
        self._wake.set()

    def stop_ap(self) -> bool:
        with self._lock:
            self._ap_restore_pending = False
            self._last_ap_action_at = self._clock.monotonic()
            result = self._network.stop_ap()
            if result:
                self._status.counters.ap_stops += 1
            self._wake.set()
            self._publish_status_locked()
            return result

    def recover_now(self) -> bool:
        with self._lock:
            self._last_reconnect_at = self._clock.monotonic()
            self._status.counters.reconnect_attempts += 1
            result = self._network.reconnect_saved()
            if result:
                self._uplink_settle_until = (
                    self._clock.monotonic() + self.config.wifi.reconnect_settle_seconds
                )
            self._wake.set()
            self._publish_status_locked()
            return result

    def _transition(self, state: AgentState, reason: str) -> None:
        if self._status.state != state or self._status.reason != reason:
            LOGGER.info("state=%s reason=%s", state.value, reason)
            self._status.state = state
            self._status.reason = reason
            self._status.state_changed_at = self._clock.time()
        if state in {AgentState.ONLINE, AgentState.PROVISIONING}:
            self._status.next_action_at = None

    def _publish_status_locked(self) -> None:
        snapshot = deepcopy(self._status)
        with self._status_view_lock:
            self._published_status = snapshot


def _network_reason(snapshot: NetworkSnapshot) -> str:
    if snapshot.error == "network_manager_unavailable":
        return snapshot.error
    if not snapshot.link_up:
        return "wifi_disconnected"
    if not snapshot.ipv4:
        return "dhcp_incomplete"
    if not snapshot.gateway:
        return "default_route_missing"
    if snapshot.captive_portal:
        return "captive_portal"
    if snapshot.error:
        return snapshot.error
    return "uplink_target_unreachable"


def _validate_wifi_credentials(ssid: str, password: str) -> str:
    if not _SAFE_SSID.fullmatch(ssid) or len(ssid.encode("utf-8")) > 32:
        return "invalid_ssid"
    if any(character in password for character in ("\x00", "\r", "\n")):
        return "invalid_wifi_password"
    if not password:
        return ""
    if _HEX_PSK.fullmatch(password):
        return ""
    password_bytes = password.encode("utf-8")
    return "" if 8 <= len(password_bytes) <= 63 else "invalid_wifi_password"
