from __future__ import annotations

import argparse
import getpass
import json
import sys
import urllib.error
import urllib.request
from typing import Any

from .config import load_config
from .preflight import run_preflight


def _request(base_url: str, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=35.0) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            detail = {"error": f"http_{exc.code}"}
        raise RuntimeError(detail.get("reason") or detail.get("error") or "request_failed") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="edgelinkctl", description="Manage EdgeLink Agent")
    parser.add_argument("--url", default="http://127.0.0.1:8787")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("scan")
    sub.add_parser("recover")
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--config", default="/etc/edgelink/config.json")
    connect = sub.add_parser("connect")
    connect.add_argument("ssid")
    connect.add_argument("--password-stdin", action="store_true")
    ap = sub.add_parser("ap")
    ap.add_argument("action", choices=("start", "stop"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "preflight":
            checks_payload: list[dict[str, object]] = [
                {
                    "name": check.name,
                    "ok": check.ok,
                    "severity": check.severity,
                    "detail": check.detail,
                }
                for check in run_preflight(load_config(args.config))
            ]
            payload: dict[str, Any] = {
                "ok": True,
                "checks": checks_payload,
            }
            payload["ok"] = all(
                bool(check["ok"])
                for check in checks_payload
                if check["severity"] == "error"
            )
        elif args.command == "status":
            payload = _request(args.url, "GET", "/v1/status")
        elif args.command == "scan":
            payload = _request(args.url, "GET", "/v1/wifi/scan")
        elif args.command == "recover":
            payload = _request(args.url, "POST", "/v1/network/recover")
        elif args.command == "ap":
            payload = _request(args.url, "POST", f"/v1/ap/{args.action}")
        else:
            password = (
                sys.stdin.readline().rstrip("\r\n")
                if args.password_stdin
                else getpass.getpass("Wi-Fi password: ")
            )
            payload = _request(
                args.url, "POST", "/v1/wifi/connect", {"ssid": args.ssid, "password": password}
            )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload.get("ok", True) else 2
    except (OSError, RuntimeError) as exc:
        print(f"edgelinkctl: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
