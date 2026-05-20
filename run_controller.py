"""Standalone CLI runner for the polling + rule controller (no UI).

Useful for leaving the rule scheduler running in the background — e.g. on
a Raspberry Pi or a headless box — without keeping Streamlit open.

    python run_controller.py --poll 30 --apply 120

Credentials are read from environment variables / .env:
    ARISTON_USERNAME (or USERNAME)
    ARISTON_PASSWORD (or PASSWORD)
    ARISTON_GATEWAY  (or MACADRESS, SERIAL_NUMBER)
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import time

from dotenv import load_dotenv

from ariston_client import AristonClient
from controller import EventLog, PollerThread, RuleThread
from rules import load_rules


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--poll", type=float, default=30.0,
                        help="Poll interval in seconds (default 30).")
    parser.add_argument("--apply", type=float, default=120.0,
                        help="Rule-application interval in seconds (default 120).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    username = os.getenv("ARISTON_USERNAME") or os.getenv("USERNAME")
    password = os.getenv("ARISTON_PASSWORD") or os.getenv("PASSWORD")
    gateway = (os.getenv("ARISTON_GATEWAY")
               or os.getenv("MACADRESS")
               or os.getenv("SERIAL_NUMBER"))
    if not username or not password:
        raise SystemExit(
            "Set ARISTON_USERNAME and ARISTON_PASSWORD in your environment or .env"
        )

    client = AristonClient()
    if not client.connect(username, password, gateway):
        raise SystemExit("Failed to bind to an Ariston device.")

    log = EventLog()

    class _PrintLog(EventLog):
        def add(self, level: str, msg: str) -> None:
            super().add(level, msg)
            logging.log(getattr(logging, level, logging.INFO), msg)

    log = _PrintLog()

    poller = PollerThread(client, log, args.poll)
    poller.start()
    ruler = RuleThread(client, poller, load_rules, log, args.apply)
    ruler.start()

    stop = False

    def _sig(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    logging.info("Controller running. Ctrl-C to exit.")
    while not stop:
        time.sleep(1.0)

    poller.stop()
    ruler.stop()
    poller.join(timeout=5)
    ruler.join(timeout=5)


if __name__ == "__main__":
    main()
