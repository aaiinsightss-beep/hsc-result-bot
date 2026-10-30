# -*- coding: utf-8 -*-
"""CLI shell — python main.py [--force-now] [--test]

Sleeps until result time (10:59:55 Asia/Dhaka), pulls until found,
pushes to Telegram, exits. --force-now skips the countdown.
"""
import argparse
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))


def _utf8_stdio() -> None:
    """cp1252 consoles kill emoji logs at the first char — kill flat."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


from engine import Engine  # noqa: E402


def main() -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--force-now", action="store_true",
                    help="skip countdown, start pulling 10s from now")
    ap.add_argument("--test", action="store_true",
                    help="ping mirrors + telegram, then exit")
    args = ap.parse_args()

    eng = Engine(log_cb=lambda msg, tag="info": print("[%s] %s" % (tag, msg)))
    if args.test:
        eng.test()
        return 0
    eng.start(force_now=args.force_now)
    eng.done.wait()
    return 0 if eng.result_sent else 1


if __name__ == "__main__":
    raise SystemExit(main())
