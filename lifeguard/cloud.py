"""Cloud side of the split deployment: cameras are remote, fed by the pool computer."""
import threading
import time

from . import registry
from . import settings as cfg
from ._offline_frame import OFFLINE_JPEG

LEVELS = ("SAFE", "WARNING", "DANGER", "OFFLINE")
EMPTY = {"people_in_pool": 0, "people_outside_pool": 0, "submerged_count": 0, "status": "OFFLINE",
         "last_updated": None}


class RemoteCamera:
    """Looks like a CameraWorker to the dashboard, but its data comes from the pool computer."""

    STALE_AFTER_SEC = 10        # no report for this long -> the pool computer is considered offline
    VIEWER_TIMEOUT_SEC = 8      # someone watched the feed this recently -> ask for live frames

    def __init__(self, cam_id):
        self.cam_id = cam_id
        self.label = cfg.CAMERAS[cam_id]
        self._cond = threading.Condition()
        self._latest, self._latest_seq = OFFLINE_JPEG, 1
        self._reported, self._alerts = None, []
        self._seen = 0.0
        self._last_viewer = 0.0

    # ---- data from the pool computer
    def update(self, report, alerts):
        def count(key):
            try:
                return max(0, int(report.get(key, 0)))
            except (TypeError, ValueError):
                return 0

        status = report.get("status")
        self._reported = {
            "people_in_pool": count("people_in_pool"),
            "people_outside_pool": count("people_outside_pool"),
            "submerged_count": count("submerged_count"),
            "status": status if status in LEVELS else "OFFLINE",
            "last_updated": str(report.get("last_updated") or "")[:19] or None,
        }
        self._alerts = [a for a in alerts if isinstance(a, dict)][:20]
        self._seen = time.time()

    def push_frame(self, jpeg):
        with self._cond:
            self._latest, self._latest_seq = jpeg, self._latest_seq + 1
            self._cond.notify_all()

    # ---- what the dashboard reads
    def _fresh(self):
        return self._reported is not None and time.time() - self._seen <= self.STALE_AFTER_SEC

    def _show_offline_picture_if_stale(self):
        if not self._fresh() and self._latest is not OFFLINE_JPEG:
            self.push_frame(OFFLINE_JPEG)            # don't leave a frozen last frame on screen

    @property
    def metrics(self):
        self._show_offline_picture_if_stale()
        if self._fresh():
            return self._reported
        return {**EMPTY, "last_updated": (self._reported or {}).get("last_updated")}

    def active_alerts(self):
        return list(self._alerts) if self._fresh() else []

    def wait_frame(self, last_seq, timeout=5):
        self._last_viewer = time.time()
        self._show_offline_picture_if_stale()
        with self._cond:
            self._cond.wait_for(lambda: self._latest_seq != last_seq, timeout)
            self._last_viewer = time.time()
            return self._latest, self._latest_seq

    def wants_frames(self):
        return time.time() - self._last_viewer < self.VIEWER_TIMEOUT_SEC

    def stop(self):
        pass


def register_cameras():
    for cam_id in cfg.CAMERAS:
        registry.workers.setdefault(cam_id, RemoteCamera(cam_id))
