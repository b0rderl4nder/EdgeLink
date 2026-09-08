import json
from pathlib import Path

import tomllib

from edgelink_agent import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_package_and_runtime_versions_match() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["version"] == __version__


def test_installer_uses_known_default_and_never_creates_random_ap_password() -> None:
    installer = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert "secrets.token_urlsafe" not in installer
    assert "EDGELINK_AP_PASSWORD=12345678" in installer
    assert "EDGELINK_AP_PASSWORD_FILE" in installer
    assert "EDGELINK_AP_PASSWORD=%s" in installer


def test_first_install_never_silently_activates_a_network_controller() -> None:
    installer = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert "START_MODE=${EDGELINK_START:-auto}" in installer
    assert '0|1|auto)' in installer
    assert "SERVICE_WAS_ENABLED=false" in installer
    assert "SERVICE_WAS_ACTIVE=false" in installer
    assert 'if [ "$SHOULD_START" = true ]; then' in installer
    assert "EDGELINK_START=1" in installer


def test_public_portal_cannot_stop_its_own_access_point() -> None:
    page = (ROOT / "edgelink_agent" / "web" / "index.html").read_text(encoding="utf-8")

    assert 'id="apStop"' not in page
    assert "'/v1/ap/stop'" not in page


def test_systemd_unit_has_bounded_restart_policy() -> None:
    unit = (ROOT / "deploy" / "edgelink-agent.service").read_text(encoding="utf-8")

    assert "StartLimitIntervalSec=60" in unit
    assert "StartLimitBurst=10" in unit
    assert "Restart=always" in unit


def test_agent_has_no_direct_write_access_to_networkmanager_profile_store() -> None:
    unit = (ROOT / "deploy" / "edgelink-agent.service").read_text(encoding="utf-8")

    assert "/etc/NetworkManager/system-connections" not in unit
    assert "/var/lib/NetworkManager" not in unit
    assert "ReadWritePaths=/run" in unit


def test_ap_password_rotation_is_local_interactive_and_never_an_api() -> None:
    script = (ROOT / "scripts" / "set_ap_password.sh").read_text(encoding="utf-8")
    installer = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert "[ ! -t 0 ]" in script
    assert "stty -echo" in script
    assert "mktemp" in script
    assert "/usr/local/sbin/edgelink-set-ap-password" in installer


def test_product_templates_never_take_over_daemon_hotspot_profile() -> None:
    for device in ("zebao0", "zebao1"):
        config = json.loads(
            (ROOT / "deploy" / "examples" / f"{device}.json").read_text(
                encoding="utf-8"
            )
        )
        assert config["access_point"]["connection_name"] == "edgelink-provisioning"
        assert config["access_point"]["connection_name"] != "Hotspot"
        assert "Hotspot" in config["access_point"]["reserved_connection_names"]
