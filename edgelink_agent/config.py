from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class WifiConfig:
    interface: str = "wlan0"
    startup_grace_seconds: float = 45.0
    reconnect_interval_seconds: float = 15.0
    reconnect_attempts_before_ap: int = 3
    ap_fallback_seconds: float = 60.0
    recovery_cooldown_seconds: float = 120.0
    stable_before_ap_stop_seconds: float = 8.0
    auto_retry_saved_profiles: bool = True
    ap_saved_retry_seconds: float = 180.0
    ap_client_hold_seconds: float = 120.0


@dataclass(frozen=True, slots=True)
class AccessPointConfig:
    enabled: bool = True
    connection_name: str = "edgelink-provisioning"
    ssid_prefix: str = "reachy-mini-ap"
    password_env: str = "EDGELINK_AP_PASSWORD"
    ipv4_cidr: str = "10.42.0.1/24"
    reuse_existing_profile: bool = False


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
    target_policy: str = "any"
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
    tick_seconds: float = 2.0
    wifi: WifiConfig = field(default_factory=WifiConfig)
    access_point: AccessPointConfig = field(default_factory=AccessPointConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    api: ApiConfig = field(default_factory=ApiConfig)


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
    wifi = _mapping(raw.get("wifi"))
    ap = _mapping(raw.get("access_point"))
    health = _mapping(raw.get("health"))
    overlay = _mapping(raw.get("overlay"))
    api = _mapping(raw.get("api"))
    defaults = AgentConfig()
    return AgentConfig(
        device_name=str(raw.get("device_name", defaults.device_name)),
        tick_seconds=float(raw.get("tick_seconds", defaults.tick_seconds)),
        wifi=WifiConfig(**{**_dataclass_values(defaults.wifi), **wifi}),
        access_point=AccessPointConfig(**{**_dataclass_values(defaults.access_point), **ap}),
        health=HealthConfig(
            dns_name=str(health.get("dns_name", defaults.health.dns_name)),
            target_policy=str(health.get("target_policy", defaults.health.target_policy)),
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


def _dataclass_values(instance: object) -> dict[str, Any]:
    return {
        name: getattr(instance, name)
        for name in getattr(instance, "__dataclass_fields__", {})
    }
