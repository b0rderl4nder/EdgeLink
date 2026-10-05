from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class AgentState(str, Enum):
    STARTING = "starting"
    ONLINE = "online"
    DEGRADED = "degraded"
    RECOVERING = "recovering"
    PROVISIONING = "provisioning"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class WifiNetwork:
    ssid: str
    signal: int = 0
    security: str = ""
    active: bool = False


@dataclass(frozen=True, slots=True)
class ConnectionAttempt:
    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True, slots=True)
class NetworkSnapshot:
    interface: str
    link_up: bool = False
    connection_name: str = ""
    ssid: str = ""
    ipv4: str = ""
    gateway: str = ""
    dns_servers: tuple[str, ...] = ()
    dns_reachable: bool = False
    target_reachable: bool = False
    https_reachable: bool = False
    connectivity_state: str = "unknown"
    health_level: str = "disconnected"
    validation_passed: bool | None = None
    captive_portal: bool = False
    ap_active: bool = False
    checked_at: float = 0.0
    error: str = ""

    @property
    def has_addressing(self) -> bool:
        return bool(self.link_up and self.ipv4 and self.gateway)

    @property
    def uplink_ready(self) -> bool:
        if self.validation_passed is not None:
            return self.validation_passed
        return self.has_addressing and self.target_reachable and not self.captive_portal


@dataclass(frozen=True, slots=True)
class OverlaySnapshot:
    enabled: bool = True
    running: bool = False
    connected: bool = False
    virtual_ip: str = ""
    peer_count: int = 0
    checked_at: float = 0.0
    error: str = ""


@dataclass(frozen=True, slots=True)
class ServiceSnapshot:
    name: str
    reachable: bool
    latency_ms: float | None = None
    error: str = ""


@dataclass(slots=True)
class Counters:
    ticks: int = 0
    uplink_failures: int = 0
    reconnect_attempts: int = 0
    ap_starts: int = 0
    ap_start_failures: int = 0
    ap_restore_retries: int = 0
    ap_stops: int = 0
    ap_reconciliations: int = 0
    overlay_starts: int = 0
    overlay_restarts: int = 0
    provisioning_successes: int = 0
    provisioning_failures: int = 0
    provisioning_rejections: int = 0
    provisioning_replacements: int = 0
    previous_network_restores: int = 0
    checkpoints_created: int = 0
    checkpoints_committed: int = 0
    checkpoints_rolled_back: int = 0
    checkpoint_fallbacks: int = 0


@dataclass(slots=True)
class ProvisioningSnapshot:
    operation_id: str = ""
    ssid: str = ""
    phase: str = "idle"
    reason: str = ""
    requested_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0


@dataclass(slots=True)
class AgentStatus:
    state: AgentState = AgentState.STARTING
    reason: str = "initializing"
    network: NetworkSnapshot = field(default_factory=lambda: NetworkSnapshot(interface=""))
    overlay: OverlaySnapshot = field(default_factory=OverlaySnapshot)
    services: tuple[ServiceSnapshot, ...] = ()
    counters: Counters = field(default_factory=Counters)
    provisioning: ProvisioningSnapshot = field(default_factory=ProvisioningSnapshot)
    state_changed_at: float = 0.0
    last_event_at: float = 0.0
    next_action_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["state"] = self.state.value
        return payload
