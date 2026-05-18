"""
Send one NSIGII message to a configured peer.

Usage:
  python send_message.py "hello from laptop A"
  python send_message.py --host 192.168.1.119 --port 9200 "hello"
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path

from nsigii_peer import NSIGIIPeer


CONFIG_PATH = Path(__file__).with_name("nsigii_config.json")


def _load_config() -> dict:
    defaults = {
        "node_id": socket.gethostname(),
        "listen_port": 9201,
        "remote_peers": [],
    }
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            defaults.update(json.load(fh))
    return defaults


def _first_configured_peer(config: dict) -> tuple[str, int]:
    for peer in config.get("remote_peers", []):
        host = str(peer.get("host", "")).strip()
        if host:
            return host, int(peer.get("port", 9200))
    raise SystemExit(
        "No remote peer configured. Add a host to nsigii_config.json or pass --host."
    )


def main() -> int:
    config = _load_config()

    parser = argparse.ArgumentParser(description="Send one NSIGII message.")
    parser.add_argument("message", help="Message text to send to the remote peer.")
    parser.add_argument("--host", help="Remote peer IP address or hostname.")
    parser.add_argument("--port", type=int, default=None, help="Remote peer port.")
    parser.add_argument(
        "--node-id",
        default=f"{config.get('node_id', socket.gethostname())}-SENDER",
        help="Temporary node id for this sender process.",
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        default=0,
        help="Temporary local listen port. Use 0 to let Windows choose one.",
    )
    args = parser.parse_args()

    if args.host:
        host = args.host
        port = args.port or 9200
    else:
        host, port = _first_configured_peer(config)

    peer = NSIGIIPeer(node_id=args.node_id, listen_port=args.listen_port)
    peer.start()
    try:
        if not peer.connect_to(host, port):
            print(f"NSIGII send failed: could not connect to {host}:{port}", file=sys.stderr)
            return 1

        peer_key = f"{host}:{port}"
        if not peer.send(peer_key, args.message.encode("utf-8")):
            print(f"NSIGII send failed: peer {peer_key} is not connected", file=sys.stderr)
            return 1

        print(f"NSIGII message sent to {peer_key}: {args.message}")
        time.sleep(0.2)
        return 0
    finally:
        peer.stop()


if __name__ == "__main__":
    raise SystemExit(main())
