from __future__ import annotations

import json

import pytest

from edgelink_agent.config import load_config


def test_config_rejects_invalid_timing_and_network_boundaries(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"wifi": {"ap_restore_retry_seconds": 0}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="positive Wi-Fi settings"):
        load_config(path)


def test_config_rejects_invalid_trusted_subnet(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"api": {"trusted_mutation_subnets": ["not-a-network"]}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid trusted subnet"):
        load_config(path)


def test_config_rejects_access_point_ssid_over_32_bytes(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"access_point": {"ssid": "中" * 11}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="SSID exceeds 32 bytes"):
        load_config(path)


def test_legacy_noop_fields_are_migrated_without_remaining_in_public_model(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "wifi": {"stable_before_ap_stop_seconds": 9},
                "access_point": {"reuse_existing_profile": True},
            }
        ),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.wifi.reconnect_settle_seconds == 9
    assert not hasattr(config.wifi, "stable_before_ap_stop_seconds")
    assert not hasattr(config.access_point, "reuse_existing_profile")


def test_config_rejects_ap_profile_owned_by_another_controller(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "access_point": {
                    "connection_name": "Hotspot",
                    "reserved_connection_names": ["Hotspot"],
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="reserved by another controller"):
        load_config(path)


def test_internet_mode_requires_https_probe_hosts(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"health": {"https_probe_hosts": []}}), encoding="utf-8")
    with pytest.raises(ValueError, match="https_probe_hosts"):
        load_config(path)


def test_portal_probe_path_rejects_http_header_injection(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"health": {"portal_probe_path": "/generate_204\r\nHost: bad"}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="portal probe path"):
        load_config(path)


def test_portal_fallback_delay_must_not_be_negative(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"wifi": {"portal_fallback_seconds": -1}}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="portal_fallback_seconds"):
        load_config(path)
