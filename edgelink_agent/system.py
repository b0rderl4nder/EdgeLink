from __future__ import annotations

import hashlib
import os
import re
import socket
import stat
import subprocess
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import AccessPointConfig, HealthConfig, OverlayConfig, WifiConfig
from .models import NetworkSnapshot, OverlaySnapshot, WifiNetwork


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner:
    """Small subprocess boundary so system operations remain fully testable."""

    def run(self, args: Sequence[str], timeout: float = 10.0) -> CommandResult:
        try:
            result = subprocess.run(
                list(args),
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, "LC_ALL": "C"},
            )
            return CommandResult(result.returncode, result.stdout, result.stderr)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CommandResult(124, "", type(exc).__name__)


class ConnectivityProbe:
    def __init__(self, config: HealthConfig) -> None:
        self._config = config

    def dns_available(self) -> bool:
        try:
            socket.getaddrinfo(self._config.dns_name, 443, type=socket.SOCK_STREAM)
            return True
        except OSError:
            return False

    def targets_available(self) -> bool:
        required = [target for target in self._config.targets if target.required]
        if not required:
            return True
        results: list[bool] = []
        for target in required:
            try:
                with socket.create_connection(
                    (target.host, target.port), timeout=target.timeout_seconds
                ):
                    results.append(True)
            except OSError:
                results.append(False)
        return all(results) if self._config.target_policy == "all" else any(results)


class NetworkManagerAdapter:
    """NetworkManager implementation with transactional AP-safe provisioning."""

    _FIELD_RE = re.compile(r"^([A-Z0-9_.\[\]-]+):\s*(.*)$")

    def __init__(
        self,
        wifi: WifiConfig,
        access_point: AccessPointConfig,
        health: HealthConfig,
        runner: CommandRunner | None = None,
    ) -> None:
        self._wifi = wifi
        self._ap = access_point
        self._runner = runner or CommandRunner()
        self._probe = ConnectivityProbe(health)

    def _run(self, *args: str, timeout: float = 10.0) -> CommandResult:
        return self._runner.run(args, timeout=timeout)

    def _device_fields(self) -> dict[str, list[str]]:
        result = self._run(
            "nmcli",
            "-f",
            "GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS",
            "device",
            "show",
            self._wifi.interface,
        )
        fields: dict[str, list[str]] = {}
        if result.returncode:
            return fields
        for line in result.stdout.splitlines():
            match = self._FIELD_RE.match(line.strip())
            if match:
                fields.setdefault(match.group(1), []).append(match.group(2).strip())
        return fields

    def _active_ssid(self) -> str:
        result = self._run(
            "nmcli", "-t", "-f", "ACTIVE,SSID", "device", "wifi", "list", "ifname",
            self._wifi.interface,
        )
        if result.returncode:
            return ""
        for line in result.stdout.splitlines():
            active, separator, ssid = line.partition(":")
            if separator and active.lower() in {"yes", "true"}:
                return _nm_unescape(ssid)
        return ""

    def _connection_mode(self, connection_name: str) -> str:
        if not connection_name or connection_name in {"--", "(none)"}:
            return ""
        result = self._run(
            "nmcli",
            "-g",
            "802-11-wireless.mode",
            "connection",
            "show",
            connection_name,
        )
        return result.stdout.strip().lower() if result.returncode == 0 else ""

    def snapshot(self) -> NetworkSnapshot:
        now = time.time()
        fields = self._device_fields()
        if not fields:
            return NetworkSnapshot(
                interface=self._wifi.interface,
                checked_at=now,
                error="network_manager_unavailable",
            )
        state = " ".join(fields.get("GENERAL.STATE", []))
        connection = _first(fields.get("GENERAL.CONNECTION"))
        connection_mode = self._connection_mode(connection)
        addresses = _clean_values([
            value
            for key, values in fields.items()
            if key.startswith("IP4.ADDRESS")
            for value in values
        ])
        gateways = _clean_values([
            value
            for key, values in fields.items()
            if key.startswith("IP4.GATEWAY")
            for value in values
        ])
        dns = _clean_values([
            value
            for key, values in fields.items()
            if key.startswith("IP4.DNS")
            for value in values
            if value
        ])
        link_up = state.startswith("100") or "connected" in state.lower()
        connectivity = self._run("nmcli", "networking", "connectivity", "check", timeout=5.0)
        connectivity_state = connectivity.stdout.strip().lower()
        addressing_ready = bool(link_up and addresses and gateways and dns)
        dns_ready = self._probe.dns_available() if addressing_ready else False
        target_ready = self._probe.targets_available() if addressing_ready and dns_ready else False
        return NetworkSnapshot(
            interface=self._wifi.interface,
            link_up=link_up,
            connection_name=connection,
            ssid=self._active_ssid(),
            ipv4=_first(addresses),
            gateway=_first(gateways),
            dns_servers=tuple(dns),
            target_reachable=target_ready,
            captive_portal=connectivity_state == "portal",
            ap_active=connection_mode == "ap" or connection == self._ap.connection_name,
            checked_at=now,
            error="" if dns_ready or not addressing_ready else "dns_unavailable",
        )

    def scan(self) -> list[WifiNetwork]:
        result = self._run(
            "nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL,SECURITY", "device", "wifi", "list",
            "--rescan", "yes", "ifname", self._wifi.interface, timeout=20.0,
        )
        if result.returncode:
            return []
        by_ssid: dict[str, WifiNetwork] = {}
        for line in result.stdout.splitlines():
            parts = _nm_split(line)
            if len(parts) < 4 or not parts[1]:
                continue
            try:
                signal = int(parts[2])
            except ValueError:
                signal = 0
            item = WifiNetwork(
                ssid=parts[1], signal=signal, security=parts[3],
                active=parts[0].lower() in {"yes", "true"},
            )
            previous = by_ssid.get(item.ssid)
            if previous is None or item.signal > previous.signal:
                by_ssid[item.ssid] = item
        return sorted(by_ssid.values(), key=lambda item: (not item.active, -item.signal, item.ssid))

    def reconnect_saved(self) -> bool:
        result = self._run(
            "nmcli", "--wait", "20", "device", "connect", self._wifi.interface, timeout=25.0
        )
        return result.returncode == 0

    def connect(self, ssid: str, password: str) -> bool:
        profile = _profile_name(ssid)
        exists = self._run("nmcli", "-g", "NAME", "connection", "show", profile).returncode == 0
        if not exists:
            created = self._run(
                "nmcli", "connection", "add", "type", "wifi", "ifname", self._wifi.interface,
                "con-name", profile, "ssid", ssid,
            )
            if created.returncode:
                return False
        base = self._run(
            "nmcli", "connection", "modify", profile,
            "connection.autoconnect", "yes",
            "connection.autoconnect-priority", "20",
            "ipv4.method", "auto", "ipv4.addresses", "", "ipv4.gateway", "", "ipv4.dns", "",
            "ipv4.ignore-auto-dns", "no", "ipv6.method", "auto",
        )
        if base.returncode:
            return False
        if password:
            security = self._run(
                "nmcli", "connection", "modify", profile,
                "802-11-wireless-security.key-mgmt", "wpa-psk",
            )
            if security.returncode:
                return False
            with _nm_secret_file(password) as secret_path:
                activated = self._run(
                    "nmcli", "--wait", "25", "connection", "up", profile,
                    "passwd-file", str(secret_path), timeout=30.0,
                )
        else:
            self._run(
                "nmcli", "connection", "modify", profile,
                "802-11-wireless-security.key-mgmt", "",
            )
            activated = self._run(
                "nmcli", "--wait", "25", "connection", "up", profile, timeout=30.0
            )
        return activated.returncode == 0

    def forget(self, connection_name: str) -> bool:
        if not connection_name or connection_name == self._ap.connection_name:
            return False
        return self._run("nmcli", "connection", "delete", connection_name).returncode == 0

    def start_ap(self, ssid: str, password: str) -> bool:
        profile = self._ap.connection_name
        exists = self._run("nmcli", "-g", "NAME", "connection", "show", profile).returncode == 0
        if exists and self._ap.reuse_existing_profile:
            return self._run(
                "nmcli", "--wait", "20", "connection", "up", profile, timeout=25.0
            ).returncode == 0
        if not exists:
            created = self._run(
                "nmcli", "connection", "add", "type", "wifi", "ifname", self._wifi.interface,
                "con-name", profile, "ssid", ssid,
            )
            if created.returncode:
                return False
        modified = self._run(
            "nmcli", "connection", "modify", profile,
            "802-11-wireless.mode", "ap",
            "802-11-wireless.band", "bg",
            "802-11-wireless-security.key-mgmt", "wpa-psk",
            "ipv4.method", "shared",
            "ipv4.addresses", self._ap.ipv4_cidr,
            "ipv6.method", "disabled",
            "connection.autoconnect", "no",
        )
        if modified.returncode:
            return False
        with _nm_secret_file(password) as secret_path:
            activated = self._run(
                "nmcli", "--wait", "20", "connection", "up", profile,
                "passwd-file", str(secret_path), timeout=25.0,
            )
        return activated.returncode == 0

    def stop_ap(self) -> bool:
        result = self._run("nmcli", "connection", "down", self._ap.connection_name)
        return result.returncode == 0 or "not active" in result.stderr.lower()


class EasyTierAdapter:
    def __init__(self, config: OverlayConfig, runner: CommandRunner | None = None) -> None:
        self._config = config
        self._runner = runner or CommandRunner()

    def snapshot(self) -> OverlaySnapshot:
        now = time.time()
        if not self._config.enabled:
            return OverlaySnapshot(enabled=False, running=False, connected=True, checked_at=now)
        active = self._runner.run(
            ("systemctl", "is-active", self._config.service_unit), timeout=5.0
        )
        running = active.returncode == 0 and active.stdout.strip() == "active"
        if not running:
            return OverlaySnapshot(checked_at=now, error="overlay_service_inactive")
        node = self._runner.run((self._config.cli_path, "node"), timeout=5.0)
        peer = self._runner.run((self._config.cli_path, "peer"), timeout=5.0)
        virtual_ip = _extract_virtual_ip(node.stdout, self._config.expected_cidr_prefix)
        peer_count = _count_connected_peers(peer.stdout)
        return OverlaySnapshot(
            running=True,
            connected=bool(virtual_ip and peer_count > 0),
            virtual_ip=virtual_ip,
            peer_count=peer_count,
            checked_at=now,
            error="" if virtual_ip else "overlay_not_joined",
        )

    def start(self) -> bool:
        return self._runner.run(
            ("systemctl", "start", self._config.service_unit), timeout=15.0
        ).returncode == 0

    def restart(self) -> bool:
        return self._runner.run(
            ("systemctl", "restart", self._config.service_unit), timeout=15.0
        ).returncode == 0


class _SecretFile:
    def __init__(self, password: str) -> None:
        self._password = password
        self.path: Path | None = None

    def __enter__(self) -> Path:
        descriptor, raw_path = tempfile.mkstemp(prefix="edgelink-nm-", text=True)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"802-11-wireless-security.psk:{self._password}\n")
        self.path = Path(raw_path)
        self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return self.path

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.path is not None:
            try:
                self.path.unlink(missing_ok=True)
            finally:
                self.path = None


def _nm_secret_file(password: str) -> _SecretFile:
    return _SecretFile(password)


def _profile_name(ssid: str) -> str:
    digest = hashlib.sha256(ssid.encode("utf-8")).hexdigest()[:10]
    return f"edgelink-{digest}"


def _first(values: list[str] | None) -> str:
    return values[0] if values else ""


def _clean_values(values: list[str]) -> list[str]:
    return [value for value in values if value and value not in {"--", "(none)", "unknown"}]


def _nm_unescape(value: str) -> str:
    return value.replace("\\:", ":").replace("\\\\", "\\")


def _nm_split(line: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    escaped = False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _extract_virtual_ip(text: str, prefix: str) -> str:
    for match in re.finditer(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text):
        if match.group(0).startswith(prefix):
            return match.group(0)
    return ""


def _count_connected_peers(text: str) -> int:
    count = 0
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3 or not re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}/\d+", cells[0]):
            continue
        if cells[2].lower() != "local":
            count += 1
    return count
