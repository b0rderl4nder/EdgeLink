from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_installer_never_creates_an_unknown_random_ap_password() -> None:
    installer = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert "secrets.token_urlsafe" not in installer
    assert "请输入设备交付用配网热点密码" in installer
    assert "EDGELINK_AP_PASSWORD_FILE" in installer
    assert "EDGELINK_AP_PASSWORD=%s" in installer


def test_public_portal_cannot_stop_its_own_access_point() -> None:
    page = (ROOT / "edgelink_agent" / "web" / "index.html").read_text(encoding="utf-8")

    assert 'id="apStop"' not in page
    assert "'/v1/ap/stop'" not in page


def test_systemd_unit_has_bounded_restart_policy() -> None:
    unit = (ROOT / "deploy" / "edgelink-agent.service").read_text(encoding="utf-8")

    assert "StartLimitIntervalSec=60" in unit
    assert "StartLimitBurst=10" in unit
    assert "Restart=always" in unit


def test_ap_password_rotation_is_local_interactive_and_never_an_api() -> None:
    script = (ROOT / "scripts" / "set_ap_password.sh").read_text(encoding="utf-8")
    installer = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert "[ ! -t 0 ]" in script
    assert "stty -echo" in script
    assert "mktemp" in script
    assert "/usr/local/sbin/edgelink-set-ap-password" in installer
