"""Drowning-alert logic, driven by a fake YOLO model so no GPU/weights are needed."""
import time
from types import SimpleNamespace

import numpy as np
import pytest

from lifeguard import db
from lifeguard import settings as cfg
from lifeguard.vision import worker as w

POOL = [[0, 0], [639, 0], [639, 479], [0, 479]]
CONFIG = {"cam1_roi": POOL}


class FakeModel:
    """Returns whatever people the test sets: [(track_id, (x1, y1, x2, y2)), ...]"""
    def __init__(self):
        self.people = []

    def track(self, *_args, **_kwargs):
        boxes = [SimpleNamespace(xyxy=[list(b)], id=None if t is None else [t]) for t, b in self.people]
        return [SimpleNamespace(boxes=boxes)]


@pytest.fixture
def rig(app, monkeypatch):
    db.set_setting("submersion_warn_sec", 1)
    db.set_setting("submersion_danger_sec", 2)
    w._settings_cache.update(at=-1e9, value=None)
    monkeypatch.setattr(cfg, "POST_ROLL_SEC", 1)
    monkeypatch.setattr(cfg, "LOST_TRACK_GRACE_SEC", 1)
    worker = w.CameraWorker("cam1")
    model = FakeModel()
    frame = lambda: np.zeros((480, 640, 3), np.uint8)
    return worker, model, frame


def step(rig, people):
    worker, model, frame = rig
    model.people = people
    worker.process(frame(), model, CONFIG)
    return worker.metrics


SWIMMER = (1, (100, 100, 160, 300))
SIDE = (2, (100, 100, 160, 300))


def test_regression_runtime_settings_exists_and_frames_process(rig):
    """The original code called an undefined runtime_settings() on every frame."""
    assert step(rig, [SWIMMER])["status"] == "SAFE"


def test_escalates_safe_warning_danger_and_creates_one_incident(rig):
    assert step(rig, [SWIMMER])["status"] == "SAFE"
    time.sleep(1.1)
    assert step(rig, [SWIMMER])["status"] == "WARNING"
    time.sleep(1.1)
    m = step(rig, [SWIMMER])
    assert m["status"] == "DANGER" and m["submerged_count"] == 1
    step(rig, [SWIMMER])                               # still in danger: no second incident
    incidents = db.list_incidents()
    assert len(incidents) == 1 and incidents[0]["cam_id"] == "cam1" and incidents[0]["status"] == "ACTIVE"
    assert rig[0].active_alerts()[0]["track_id"] == 1


def test_leaving_the_pool_resolves_the_alert(rig):
    step(rig, [SWIMMER]); time.sleep(2.2); step(rig, [SWIMMER])
    assert db.list_incidents()[0]["status"] == "ACTIVE"
    far = (1, (700, 100, 760, 300))                    # feet outside the polygon
    m = step(rig, [far])
    assert m["people_outside_pool"] == 1 and m["status"] == "SAFE"
    assert db.list_incidents()[0]["status"] == "RESOLVED"


def test_brief_detection_dropout_keeps_the_timer(rig):
    """A swimmer who is momentarily not detected must not have their clock reset."""
    step(rig, [SWIMMER]); time.sleep(0.6)
    step(rig, [])                                      # lost, but inside the 1 s grace window
    time.sleep(0.6)
    assert step(rig, [SWIMMER])["status"] == "WARNING"  # 1.2 s since first seen: clock was kept


def test_long_dropout_forgets_the_swimmer(rig):
    step(rig, [SWIMMER]); time.sleep(1.1)
    step(rig, [])
    time.sleep(1.2)                                    # longer than LOST_TRACK_GRACE_SEC
    step(rig, [])
    assert 1 not in rig[0].timers
    assert step(rig, [SWIMMER])["status"] == "SAFE"    # clock restarted


def test_poolside_people_are_counted_separately(rig):
    m = step(rig, [SWIMMER, (3, (800, 100, 860, 300))])
    assert (m["people_in_pool"], m["people_outside_pool"]) == (1, 1)


def test_untracked_detection_does_not_crash(rig):
    assert step(rig, [(None, (100, 100, 160, 300))])["people_in_pool"] == 1


def test_offline_clears_tracking_and_resolves_incident(rig):
    step(rig, [SWIMMER]); time.sleep(2.2); step(rig, [SWIMMER])
    rig[0].set_offline(["offline"])
    assert rig[0].metrics["status"] == "OFFLINE" and not rig[0].active
    assert db.list_incidents()[0]["status"] == "RESOLVED"


def test_incident_video_is_attached_after_post_roll(rig):
    step(rig, [SWIMMER]); time.sleep(2.2)
    for _ in range(4):
        step(rig, [SWIMMER]); time.sleep(0.4)          # runs past the 1 s post-roll
    for _ in range(50):                                # video encoding runs in a thread
        state = db.list_incidents()[0]["video_state"]
        if state != "RECORDING":
            break
        time.sleep(0.1)
    assert state in ("READY", "FAILED")                # FAILED only if this machine has no MP4 encoder
    if state == "READY":
        blob, mime = db.get_incident_blob(db.list_incidents()[0]["id"], "video")
        assert mime == "video/mp4" and len(blob) > 1000


def test_invalid_thresholds_fall_back_to_defaults(app):
    db.set_setting("submersion_warn_sec", 90)
    db.set_setting("submersion_danger_sec", 10)
    w._settings_cache.update(at=-1e9, value=None)
    s = w.runtime_settings()
    assert (s["warn"], s["danger"]) == (cfg.DEFAULT_WARN_SEC, cfg.DEFAULT_DANGER_SEC)


def test_polygon_helper():
    assert w.is_inside_polygon((10, 10), POOL)
    assert not w.is_inside_polygon((700, 10), POOL)
    assert not w.is_inside_polygon((10, 10), [[0, 0], [1, 1]])
