from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass

from .config import AgentConfig, load_config
from .system import CommandRunner


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    severity: str
    detail: str


def run_preflight(config: AgentConfig, runner: CommandRunner | None = None) -> list[Check]:
    command = runner or CommandRunner()
    checks: list[Check] = []

    manager = command.run(("systemctl", "is-active", "NetworkManager.service"), timeout=5.0)
    checks.append(
        Check(
            "network_manager",
            manager.returncode == 0 and manager.stdout.strip() == "active",
            "error",
            "NetworkManager运行中" if manager.returncode == 0 else "NetworkManager未运行",
        )
    )

    checkpoint = command.run(
        (
            "busctl",
            "introspect",
            "org.freedesktop.NetworkManager",
            "/org/freedesktop/NetworkManager",
            "org.freedesktop.NetworkManager",
        ),
        timeout=5.0,
    )
    checkpoint_supported = (
        checkpoint.returncode == 0 and "CheckpointCreate" in checkpoint.stdout
    )
    checks.append(
        Check(
            "network_checkpoint",
            checkpoint_supported,
            "warning",
            "NetworkManager支持原子检查点回滚"
            if checkpoint_supported
            else "NetworkManager不支持检查点，将使用旧连接与AP双重回退",
        )
    )

    device = command.run(
        ("nmcli", "-g", "GENERAL.STATE", "device", "show", config.wifi.interface),
        timeout=5.0,
    )
    checks.append(
        Check(
            "wifi_interface",
            device.returncode == 0,
            "error",
            f"无线接口{config.wifi.interface}可管理"
            if device.returncode == 0
            else f"找不到无线接口{config.wifi.interface}",
        )
    )

    ap = command.run(
        (
            "nmcli",
            "-g",
            "WIFI-PROPERTIES.AP",
            "device",
            "show",
            config.wifi.interface,
        ),
        timeout=5.0,
    )
    ap_supported = ap.returncode == 0 and ap.stdout.strip().lower() in {"yes", "true", "1"}
    checks.append(
        Check(
            "ap_capability",
            ap_supported,
            "error",
            "无线网卡支持AP模式" if ap_supported else "无线网卡未报告AP能力",
        )
    )

    if config.overlay.enabled:
        unit = command.run(
            ("systemctl", "cat", config.overlay.service_unit), timeout=5.0
        )
        checks.append(
            Check(
                "overlay_unit",
                unit.returncode == 0,
                "error",
                f"已找到{config.overlay.service_unit}"
                if unit.returncode == 0
                else f"未找到{config.overlay.service_unit}",
            )
        )

    reachy = command.run(
        ("systemctl", "is-active", "reachy-mini-daemon.service"), timeout=5.0
    )
    if reachy.returncode == 0 and reachy.stdout.strip() == "active":
        # A product daemon may own a physical fallback AP.  Refuse a first
        # EdgeLink activation unless the operator has explicitly declared a
        # different reserved profile name; otherwise a harmless-looking
        # update can silently turn into two controllers writing one hotspot.
        reserved_names = tuple(
            name.strip()
            for name in config.access_point.reserved_connection_names
            if name.strip()
        )
        boundary_declared = bool(reserved_names) and (
            config.access_point.connection_name not in reserved_names
        )
        checks.append(
            Check(
                "daemon_ap_boundary",
                boundary_declared,
                "error",
                "已声明daemon保留热点，EdgeLink将使用独立AP配置"
                if boundary_declared
                else "检测到Reachy daemon，但未声明其保留热点；拒绝启用EdgeLink自动AP",
            )
        )
        checks.append(
            Check(
                "existing_network_controller",
                True,
                "warning",
                "检测到Reachy daemon；保留为独立兜底，EdgeLink不会禁用其恢复能力",
            )
        )

    return checks


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="edgelink-preflight")
    parser.add_argument("--config", default="/etc/edgelink/config.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    checks = run_preflight(load_config(args.config))
    print(json.dumps([asdict(check) for check in checks], ensure_ascii=False, indent=2))
    return 0 if all(check.ok for check in checks if check.severity == "error") else 2


if __name__ == "__main__":
    raise SystemExit(main())
