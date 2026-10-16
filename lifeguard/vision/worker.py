"""Per-camera detection worker.

Each worker owns one YOLO model (so tracker ids never mix between cameras),
reads frames, decides who is inside the pool and for how long, raises
incidents with pre/post-roll footage, and publishes annotated JPEGs for the
dashboard's live view.
"""
import logging
import sys
import threading
import time
from collections import deque

import cv2
import numpy as np

from .. import db, footage, uplink
from .. import settings as cfg
from ..registry import workers
from .stream import ESP32StreamReader
from .video import frames_to_video, jpeg_bytes, text_frame

log = logging.getLogger("lifeguard.worker")

# Alert levels, in escalation order. Colours are BGR (OpenCV).
SAFE, WARNING, DANGER, OFFLINE = "SAFE", "WARNING", "DANGER", "OFFLINE"
LEVEL_COLOR = {SAFE: (0, 255, 0), WARNING: (0, 165, 255), DANGER: (0, 0, 255)}
POOLSIDE_COLOR = (255, 200, 0)
EVENT_NAME = "Drowning Risk Detected"


# ------------------------------------------------------------------ settings
_settings_cache = {"at": -1e9, "value": None}


def runtime_settings(ttl=5.0):
    """Escalation thresholds from the database, cached so frames never wait on SQLite."""
    now = time.monotonic()
    if _settings_cache["value"] is None or now - _settings_cache["at"] > ttl:
        try:
            warn = db.get_setting_int("submersion_warn_sec", cfg.DEFAULT_WARN_SEC)
            danger = db.get_setting_int("submersion_danger_sec", cfg.DEFAULT_DANGER_SEC)
            if not 0 < warn < danger:
                log.warning("Invalid thresholds (warn=%s, danger=%s); using defaults", warn, danger)
                warn, danger = cfg.DEFAULT_WARN_SEC, cfg.DEFAULT_DANGER_SEC
            _settings_cache["value"] = {
                "warn": warn,
                "danger": danger,
                "occupancy_interval": db.get_setting_int(
                    "occupancy_log_interval_sec", cfg.DEFAULT_OCCUPANCY_INTERVAL_SEC),
            }
        except Exception as exc:
            log.warning("Could not read settings (%s); keeping previous values", exc)
            if _settings_cache["value"] is None:
                _settings_cache["value"] = {"warn": cfg.DEFAULT_WARN_SEC,
                                            "danger": cfg.DEFAULT_DANGER_SEC,
                                            "occupancy_interval": cfg.DEFAULT_OCCUPANCY_INTERVAL_SEC}
        _settings_cache["at"] = now
    return _settings_cache["value"]


def level_for(elapsed, warn, danger):
    if elapsed is None or elapsed < warn:
        return SAFE
    return WARNING if elapsed < danger else DANGER


def is_inside_polygon(point, polygon):
    if not polygon or len(polygon) < 3:
        return False
    contour = np.array(polygon, np.int32)
    return cv2.pointPolygonTest(contour, (float(point[0]), float(point[1])), False) >= 0


def draw_pool_area(frame, polygon, color):
    """Outline the monitored pool area and tint it softly so it's visible even when the
    polygon hugs (or extends past) the edges of the image."""
    pts = np.array(polygon, np.int32)
    height, width = frame.shape[:2]
    x, y, w, h = cv2.boundingRect(pts)
    x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w, width), min(y + h, height)
    if x1 > x0 and y1 > y0:                       # blend only the polygon's bounding box (fast)
        region = frame[y0:y1, x0:x1]
        overlay = region.copy()
        cv2.fillPoly(overlay, [pts - np.array([x0, y0], np.int32)], color)
        cv2.addWeighted(overlay, 0.14, region, 0.86, 0, dst=region)
    cv2.polylines(frame, [pts], True, color, 3)


# -------------------------------------------------------------- frame source
class FrameSource:
    """ESP32 stream with an optional local-webcam fallback; follows config.json changes."""

    OFFLINE_AFTER_SEC = 5
    FALLBACK_AFTER_SEC = 8
    WEBCAM_RETRY_SEC = 10
    WEBCAM_MAX_READ_FAILS = 50

    def __init__(self, cam_id):
        self.cam_id = cam_id
        self.started = time.time()
        self.stream = None
        self.url = None
        self.webcam = None
        self.last_error = None          # short text shown on the offline placeholder frame
        self._last_seq = -1
        self._webcam_retry_at = 0.0
        self._webcam_read_fails = 0

    def _follow_config(self, config):
        url = config.get(self.cam_id) or cfg.DEFAULT_STREAMS.get(self.cam_id)
        if url != self.url:
            if self.stream:
                self.stream.release()
            self.url = url
            self.stream = ESP32StreamReader(url) if url else None
            self._last_seq = -1
            log.info("[%s] connecting to %s", self.cam_id, url)

    @staticmethod
    def _open_webcam(index):
        # DirectShow is far more reliable than the default backend on Windows laptops.
        backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
        cap = cv2.VideoCapture(int(index), backend)
        if cap.isOpened():
            return cap
        cap.release()
        return None

    def _drop_webcam(self, index, reason):
        if self.webcam is not None:
            self.webcam.release()
        self.webcam = None
        self._webcam_read_fails = 0
        self._webcam_retry_at = time.time() + self.WEBCAM_RETRY_SEC
        self.last_error = f"Webcam {index}: {reason}"
        log.error("[%s] webcam %s: %s. Close other apps using the camera, check Windows "
                  "Settings > Privacy > Camera, or try another index in config.json. "
                  "Retrying in %ds.", self.cam_id, index, reason, self.WEBCAM_RETRY_SEC)

    def next(self, config):
        """-> ("frame", img) | ("idle", None) nothing new yet | ("offline", None)"""
        self._follow_config(config)
        index = config.get(f"{self.cam_id}_webcam_fallback")
        now = time.time()

        if self.webcam is None:
            frame, seq, age = (self.stream.read(self._last_seq) if self.stream
                               else (None, 0, float("inf")))
            if (age > self.FALLBACK_AFTER_SEC and index is not None
                    and now - self.started > self.FALLBACK_AFTER_SEC
                    and now >= self._webcam_retry_at):
                self.webcam = self._open_webcam(index)
                if self.webcam is None:
                    self._drop_webcam(index, "could not be opened")
                else:
                    log.warning("[%s] no ESP32 stream - using local webcam %s", self.cam_id, index)
                    self.last_error = None

        if self.webcam is not None:
            ok, frame = self.webcam.read()
            if ok and frame is not None:
                self._webcam_read_fails = 0
                return "frame", frame
            self._webcam_read_fails += 1
            if self._webcam_read_fails >= self.WEBCAM_MAX_READ_FAILS:
                self._drop_webcam(index, "opened but returns no images")
                return "offline", None
            return "idle", None
        if age > self.OFFLINE_AFTER_SEC:
            return "offline", None
        if frame is None:
            return "idle", None
        self._last_seq = seq
        return "frame", frame

    def close(self):
        if self.stream:
            self.stream.release()
        if self.webcam:
            self.webcam.release()


# -------------------------------------------------------------------- worker
class CameraWorker(threading.Thread):
    def __init__(self, cam_id):
        super().__init__(daemon=True, name=f"worker-{cam_id}")
        self.cam_id = cam_id
        self.label = cfg.CAMERAS[cam_id]
        self._halt = threading.Event()

        # latest annotated frame, shared with every browser watching the feed
        self._cond = threading.Condition()
        self._latest = jpeg_bytes(text_frame(["Starting camera..."]))
        self._latest_seq = 1

        self.metrics = {"people_in_pool": 0, "people_outside_pool": 0, "submerged_count": 0,
                        "status": OFFLINE, "last_updated": None}

        self.timers = {}       # track_id -> first time seen inside the pool
        self.last_seen = {}    # track_id -> last time seen inside the pool
        self.active = {}       # track_id -> incident id (None while being created)
        self.total_alerts = 0

        self._buffer = deque()      # (timestamp, jpeg) pre-roll
        self._recording = None      # {"ids": [...], "frames": [...], "end_at": ts}
        self._last_occupancy_log = 0.0

    # ---- live feed for browsers
    def _publish(self, jpg):
        with self._cond:
            self._latest, self._latest_seq = jpg, self._latest_seq + 1
            self._cond.notify_all()

    def latest_jpeg(self):
        """-> (jpeg_bytes, sequence_number) of the newest annotated frame (used by the cloud uplink)."""
        with self._cond:
            return self._latest, self._latest_seq

    def wait_frame(self, last_seq, timeout=5):
        with self._cond:
            self._cond.wait_for(lambda: self._latest_seq != last_seq, timeout)
            return self._latest, self._latest_seq

    def set_offline(self, lines):
        for track_id in list(self.timers) + [t for t in self.active if t not in self.timers]:
            self._forget(track_id)
        self.metrics = {"people_in_pool": 0, "people_outside_pool": 0, "submerged_count": 0,
                        "status": OFFLINE, "last_updated": self.metrics.get("last_updated")}
        self._publish(jpeg_bytes(text_frame(lines)))

    def active_alerts(self):
        now = time.time()
        return [{"id": iid, "cam_id": self.cam_id, "track_id": tid, "event": EVENT_NAME,
                 "elapsed_sec": int(now - self.timers.get(tid, now))}
                for tid, iid in list(self.active.items())]

    def stop(self):
        self._halt.set()

    # ---- incidents
    def _start_incident(self, track_id, elapsed, snapshot, now):
        incident_id = footage.record_incident(self.cam_id, track_id, elapsed, snapshot, EVENT_NAME)
        self.active[track_id] = incident_id
        uplink.enqueue("incident", incident_id)
        self.total_alerts += 1
        log.warning("[%s] ALERT: incident #%s saved, recording video", self.cam_id, incident_id)
        if self._recording is None:
            self._recording = {"ids": [incident_id], "frames": list(self._buffer),
                               "end_at": now + cfg.POST_ROLL_SEC}
        else:
            self._recording["ids"].append(incident_id)

    def _finish_recording(self):
        recording, self._recording = self._recording, None

        def save():
            try:
                video, mime = frames_to_video(recording["frames"])
                footage.store_video(recording["ids"], self.cam_id, video, mime)
                for incident_id in recording["ids"]:
                    uplink.enqueue("video", incident_id)
                log.info("[%s] video saved for incident(s) %s (%d KB)", self.cam_id,
                         recording["ids"], len(video) // 1024 if video else 0)
            except Exception:
                log.exception("[%s] could not save incident video", self.cam_id)
                db.attach_video(recording["ids"], None, None)

        threading.Thread(target=save, daemon=True, name=f"video-{self.cam_id}").start()

    def _forget(self, track_id):
        """Stop tracking someone and close their incident, if they had one."""
        self.timers.pop(track_id, None)
        self.last_seen.pop(track_id, None)
        incident_id = self.active.pop(track_id, None)
        if incident_id:
            try:
                db.resolve_incident(incident_id)
                uplink.enqueue("resolve", incident_id)
            except Exception:
                log.exception("[%s] could not resolve incident %s", self.cam_id, incident_id)

    # ---- main loop
    def run(self):
        try:
            from ultralytics import YOLO      # heavy import, only needed by the workers
            model = YOLO(cfg.MODEL_PATH)
        except Exception as exc:
            log.error("[%s] could not load YOLO model: %s", self.cam_id, exc)
            self.set_offline(["AI model failed to load", str(exc)[:60]])
            return

        source = FrameSource(self.cam_id)
        try:
            while not self._halt.is_set():
                try:
                    config = cfg.load_config()
                    kind, frame = source.next(config)
                    if kind == "offline":
                        self.set_offline([f"{self.label} OFFLINE", source.last_error or "Reconnecting..."])
                        self._halt.wait(1)
                    elif kind == "idle":
                        self._halt.wait(0.01)
                    else:
                        self.process(frame, model, config)
                except Exception:
                    log.exception("[%s] frame processing error", self.cam_id)
                    self._halt.wait(0.5)
        finally:
            source.close()

    # ---- per-frame logic
    def _detect(self, frame, model):
        """-> [(track_id, (x1, y1, x2, y2))] for every person found."""
        results = model.track(frame, classes=[0], conf=cfg.DETECTION_CONF, persist=True,
                              tracker="bytetrack.yaml", verbose=False)
        people = []
        if results and results[0].boxes is not None:
            for box in results[0].boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                # Untracked detections get a negative pseudo-id that can't clash with real ones.
                track_id = int(box.id[0]) if box.id is not None else -(x1 + y1 + 1)
                people.append((track_id, (x1, y1, x2, y2)))
        return people

    def process(self, frame, model, config):
        polygon = config.get(f"{self.cam_id}_roi", [])
        settings = runtime_settings()
        warn, danger = settings["warn"], settings["danger"]
        now = time.time()

        inside = outside = submerged = 0
        max_elapsed = None
        seen, new_alerts = set(), []

        for track_id, (x1, y1, x2, y2) in self._detect(frame, model):
            seen.add(track_id)
            feet = ((x1 + x2) // 2, y2)

            if is_inside_polygon(feet, polygon):
                inside += 1
                self.last_seen[track_id] = now
                elapsed = now - self.timers.setdefault(track_id, now)
                max_elapsed = elapsed if max_elapsed is None else max(max_elapsed, elapsed)
                level = level_for(elapsed, warn, danger)
                if level == DANGER:
                    submerged += 1
                    if track_id not in self.active:
                        new_alerts.append((track_id, elapsed))
                        self.active[track_id] = None        # reserve: never alert twice
                color = LEVEL_COLOR[level]
                label = ("IN POOL" if level == SAFE else f"{level} ({int(elapsed)}s)")
            else:
                if track_id in self.timers:
                    self._forget(track_id)
                outside += 1
                color, label = POOLSIDE_COLOR, "poolside"

            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            cv2.putText(frame, f"ID #{track_id} - {label}", (x1, max(y1 - 10, 20)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

        # Someone who vanishes (splash, under the surface) keeps their timer for a
        # short grace period; only after that do we assume they left the pool.
        for track_id in list(self.timers):
            if track_id not in seen and now - self.last_seen.get(track_id, 0) > cfg.LOST_TRACK_GRACE_SEC:
                self._forget(track_id)

        status = level_for(max_elapsed, warn, danger)
        if len(polygon) >= 3:
            draw_pool_area(frame, polygon, LEVEL_COLOR[status])
        if self._recording:
            cv2.circle(frame, (frame.shape[1] - 20, 20), 8, (0, 0, 255), -1)
            cv2.putText(frame, "REC", (frame.shape[1] - 65, 26),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)

        jpg = jpeg_bytes(frame)
        self._record(jpg, new_alerts, now)
        self._log_occupancy(inside, outside, submerged, now, settings["occupancy_interval"])

        self.metrics = {"people_in_pool": inside, "people_outside_pool": outside,
                        "submerged_count": submerged, "status": status,
                        "last_updated": time.strftime("%Y-%m-%d %H:%M:%S")}
        self._publish(jpg)

    def _record(self, jpg, new_alerts, now):
        """Rolling pre-roll buffer, incident creation and post-roll capture."""
        self._buffer.append((now, jpg))
        while self._buffer and now - self._buffer[0][0] > cfg.PRE_ROLL_SEC:
            self._buffer.popleft()

        was_recording = self._recording is not None
        for track_id, elapsed in new_alerts:
            try:
                self._start_incident(track_id, elapsed, jpg, now)
            except Exception:
                log.exception("[%s] could not save incident", self.cam_id)
                self.active.pop(track_id, None)

        if self._recording:
            if was_recording:   # a recording begun this frame already holds it via the buffer
                self._recording["frames"].append((now, jpg))
            if now >= self._recording["end_at"]:
                self._finish_recording()

    def _log_occupancy(self, inside, outside, submerged, now, interval):
        if interval <= 0 or now - self._last_occupancy_log < interval:
            return
        self._last_occupancy_log = now
        try:
            db.log_occupancy(self.cam_id, inside, outside, submerged)
        except Exception:
            log.exception("[%s] could not log occupancy", self.cam_id)


# ------------------------------------------------------------------ registry
def start_workers():
    for cam_id in cfg.CAMERAS:
        if cam_id not in workers:
            workers[cam_id] = CameraWorker(cam_id)
            workers[cam_id].start()


def stop_workers():
    for worker in workers.values():
        worker.stop()
