"""Start/stop lines in pool_events.log (kept from earlier versions)."""
import time

from . import settings as cfg


def write_event(text):
    try:
        with open(cfg.EVENT_LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] === {text} ===\n")
    except OSError:
        pass
