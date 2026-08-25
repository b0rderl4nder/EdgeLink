from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class WifiConfig:
    interface: str = "wlan0"
    startup_grace_seconds: float = 45.0
    reconnect_interval_seconds: float = 15.0
    reconnect_settle_seconds: float = 12.0
    reconnect_attempts_before_ap: int = 3
    ap_fallback_seconds: float = 60.0
    recovery_cooldown_seconds: float = 120.0
    ap_restore_retry_seconds: float = 3.0
    provisioning_start_delay_seconds: float = 1.0
    auto_retry_saved_profiles: bool = True
    ap_saved_retry_seconds: float = 180.0
    ap_client_hold_seconds: float = 120.0
    checkpoint_timeout_seconds: int = 90


@dataclass(frozen=True, slots=True)
class AccessPointConfig:
    enabled: bool = True
    connection_name: str = "edgelink-provisioning"
    ssid: str = ""
    ssid_prefix: str = "EdgeLink"
    password_env: str = "EDGELINK_AP_PASSWORD"
    ipv4_cidr: str = "10.42.0.1/24"
    reserved_connection_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProbeTarget:
    name: str
    host: str
    port: int
    required: bool = True
    timeout_seconds: float = 1.5


@dataclass(frozen=True, slots=True)
class HealthConfig:
    dns_name: str = "example.com"
    dns_timeout_seconds: float = 2.0
    target_policy: str = "any"
    minimum_connectivity: str = "internet"
    accept_dns_as_internet_evidence: bool = True
    targets: tuple[ProbeTarget, ...] = (
        ProbeTarget(name="dns_https", host="1.1.1.1", port=443),
    )


@dataclass(frozen=True, slots=True)
class OverlayConfig:
    enabled: bool = True
    kind: str = "easytier"
    service_unit: str = "easytier@default.service"
    cli_path: str = "/usr/sbin/easytier-cli"
    expected_cidr_prefix: str = "192.167."
    disconnected_restart_seconds: float = 20.0


@dataclass(frozen=True, slots=True)
class ApiConfig:
    listen_host: str = "0.0.0.0"
    listen_port: int = 8787
    trusted_mutation_subnets: tuple[str, ...] = ("127.0.0.0/8", "10.42.0.0/24")


@dataclass(frozen=True, slots=True)
class AgentConfig:
    device_name: str = "edge-device"
    # NetworkManager dispatcher events wake the controller immediately.  A slower
    # periodic audit avoids continuously spawning nmcli/DNS/overlay probes on small
    # edge CPUs without delaying real link changes.
    tick_seconds: float = 5.0
    wifi: WifiConfig = field(default_factory=WifiConfig)
    access_point: AccessPointConfig = field(default_factory=AccessPointConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    api: ApiConfig = field(default_factory=ApiConfig)


def access_point_ssid(config: AgentConfig) -> str:
    """Return the configured product SSID without exposing implementation brands."""
    exact = " ".join(config.access_point.ssid.split())[:32]
    if exact:
        return exact
    prefix = re.sub(r"[^a-zA-Z0-9-]", "-", config.access_point.ssid_prefix).strip("-")
    device = re.sub(r"[^a-zA-Z0-9-]", "-", config.device_name).strip("-").lower()
    return f"{prefix or 'EdgeLink'}-{(device[-16:] or 'device')}"[:32]


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _targets(value: object) -> tuple[ProbeTarget, ...]:
    if not isinstance(value, list):
        return HealthConfig().targets
    result: list[ProbeTarget] = []
    for item in value:
        raw = _mapping(item)
        try:
            result.append(
                ProbeTarget(
                    name=str(raw["name"]),
                    host=str(raw["host"]),
                    port=int(raw["port"]),
                    required=bool(raw.get("required", True)),
                    timeout_seconds=float(raw.get("timeout_seconds", 1.5)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(result) or HealthConfig().targets


def load_config(path: str | os.PathLike[str]) -> AgentConfig:
    config_path = Path(path)
    raw = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    wifi = dict(_mapping(raw.get("wifi")))
    ap = dict(_mapping(raw.get("access_point")))
    legacy_stable_seconds = wifi.pop("stable_before_ap_stop_seconds", None)
    if "reconnect_settle_seconds" not in wifi and legacy_stable_seconds is not None:
        wifi["reconnect_settle_seconds"] = legacy_stable_seconds
    ap.pop("reuse_existing_profile", None)
    if "reserved_connection_names" in ap:
        value = ap["reserved_connection_names"]
        ap["reserved_connection_names"] = tuple(
            str(item) for item in value
        ) if isinstance(value, list) else ()
    health = _mapping(raw.get("health"))
    overlay = _mapping(raw.get("overlay"))
    api = _mapping(raw.get("api"))
    defaults = AgentConfig()
    result = AgentConfig(
        device_name=str(raw.get("device_name", defaults.device_name)),
        tick_seconds=float(raw.get("tick_seconds", defaults.tick_seconds)),
        wifi=WifiConfig(**{**_dataclass_values(defaults.wifi), **wifi}),
        access_point=AccessPointConfig(**{**_dataclass_values(defaults.access_point), **ap}),
        health=HealthConfig(
            dns_name=str(health.get("dns_name", defaults.health.dns_name)),
            dns_timeout_seconds=float(
                health.get("dns_timeout_seconds", defaults.health.dns_timeout_seconds)
            ),
            target_policy=str(health.get("target_policy", defaults.health.target_policy)),
            minimum_connectivity=str(
                health.get("minimum_connectivity", defaults.health.minimum_connectivity)
            ),
            accept_dns_as_internet_evidence=bool(
                health.get(
                    "accept_dns_as_internet_evidence",
                    defaults.health.accept_dns_as_internet_evidence,
                )
            ),
            targets=_targets(health.get("targets")),
        ),
        overlay=OverlayConfig(**{**_dataclass_values(defaults.overlay), **overlay}),
        api=ApiConfig(
            listen_host=str(api.get("listen_host", defaults.api.listen_host)),
            listen_port=int(api.get("listen_port", defaults.api.listen_port)),
            trusted_mutation_subnets=tuple(
                str(item)
                for item in api.get(
                    "trusted_mutation_subnets", defaults.api.trusted_mutation_subnets
                )
            ),
        ),
    )
    _validate_config(result)
    return result


def _dataclass_values(instance: object) -> dict[str, Any]:
    return {
        name: getattr(instance, name)
        for name in getattr(instance, "__dataclass_fields__", {})
    }


def _validate_config(config: AgentConfig) -> None:
    positive_wifi_values = {
        "reconnect_interval_seconds": config.wifi.reconnect_interval_seconds,
        "reconnect_settle_seconds": config.wifi.reconnect_settle_seconds,
        "ap_restore_retry_seconds": config.wifi.ap_restore_retry_seconds,
        "provisioning_start_delay_seconds": config.wifi.provisioning_start_delay_seconds,
        "ap_saved_retry_seconds": config.wifi.ap_saved_retry_seconds,
        "ap_client_hold_seconds": config.wifi.ap_client_hold_seconds,
        "checkpoint_timeout_seconds": config.wifi.checkpoint_timeout_seconds,
    }
    invalid = [name for name, value in positive_wifi_values.items() if value <= 0]
    if invalid:
        raise ValueError(f"invalid positive Wi-Fi settings: {', '.join(invalid)}")
    if not 0 <= config.wifi.reconnect_attempts_before_ap <= 100:
        raise ValueError("reconnect_attempts_before_ap must be between 0 and 100")
    if config.tick_seconds <= 0:
        raise ValueError("tick_seconds must be positive")
    if config.api.listen_port < 0 or config.api.listen_port > 65535:
        raise ValueError("api.listen_port must be between 0 and 65535")
    if config.health.target_policy not in {"any", "all"}:
        raise ValueError("health.target_policy must be 'any' or 'all'")
    if config.health.minimum_connectivity not in {"local", "internet"}:
        raise ValueError("health.minimum_connectivity must be 'local' or 'internet'")
    if config.health.dns_timeout_seconds <= 0:
        raise ValueError("health.dns_timeout_seconds must be positive")
    for target in config.health.targets:
        if not 1 <= target.port <= 65535 or target.timeout_seconds <= 0:
            raise ValueError(f"invalid health target: {target.name}")
    if len(access_point_ssid(config).encode("utf-8")) > 32:
        raise ValueError("access point SSID exceeds 32 bytes")
    try:
        ipaddress.ip_interface(config.access_point.ipv4_cidr)
    except ValueError as exc:
        raise ValueError("invalid access point IPv4 CIDR") from exc
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", config.access_point.password_env):
        raise ValueError("invalid access point password environment variable")
    if config.access_point.connection_name in config.access_point.reserved_connection_names:
        raise ValueError("access point connection name is reserved by another controller")
    for subnet in config.api.trusted_mutation_subnets:
        try:
            ipaddress.ip_network(subnet)
        except ValueError as exc:
            raise ValueError(f"invalid trusted subnet: {subnet}") from exc
