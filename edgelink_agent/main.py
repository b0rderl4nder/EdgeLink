from __future__ import annotations

import argparse
import logging
import signal
import threading

from .api import ApiServer
from .config import load_config
from .controller import EdgeLinkController
from .system import EasyTierAdapter, NetworkManagerAdapter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="edgelink-agent")
    parser.add_argument("--config", default="/etc/edgelink/config.json")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--once", action="store_true", help="Run one health evaluation and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = load_config(args.config)
    network = NetworkManagerAdapter(config.wifi, config.access_point, config.health)
    overlay = EasyTierAdapter(config.overlay)
    controller = EdgeLinkController(config, network, overlay)
    if args.once:
        status = controller.tick()
        print(status.as_dict())
        return 0 if status.network.uplink_ready else 2

    api = ApiServer(config, controller, network)
    api.start()
    stopped = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        if not stopped.is_set():
            stopped.set()
            controller.stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        controller.run()
    finally:
        api.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
