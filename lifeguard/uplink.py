"""Pool computer -> cloud server.

While the system runs at the pool, this sends three things to the cloud copy of LifeGuard:

* live numbers every 2 s (people counts, status) - the cloud answers which cameras someone is
  watching, and only then are live pictures sent (a few small frames per second);
* every alert: snapshot first, then the video clip, then "resolved";
* all of it survives a dropped connection: alerts wait in a queue stored in the database and are
  delivered in order once the internet is back.

Configure with a "cloud" block in config.json (or LIFEGUARD_CLOUD_URL / LIFEGUARD_CLOUD_KEY):
    "cloud": {"url": "https://your-app.onrender.com", "key": "<LIFEGUARD_INGEST_KEY from Render>"}
"""
import json
import logging
import threading
import time

import requests

from . import db, footage, registry
from . import settings as cfg

log = logging.getLogger("lifeguard.uplink")

HEARTBEAT_SEC = 2
RETRY_STATUSES = {401, 403, 408, 425, 429}      # plus every 5xx: try again later


class UplinkError(Exception):
    def __init__(self, message, temporary):
        super().__init__(message)
        self.temporary = temporary


def enabled():
    return cfg.cloud_settings() is not None


def enqueue(kind, incident_id):
    """Queue an alert event ('incident', 'video' or 'resolve') for delivery. No-op when not configured."""
    if not enabled():
        return
    try:
        db.outbox_add(kind, incident_id)
    except Exception:
        log.exception("could not queue %s for incident %s", kind, incident_id)


def shrink(jpeg, width):
    """Smaller, lighter copy of a frame for the internet."""
    import cv2
    import numpy as np
    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return jpeg
    h, w = image.shape[:2]
    if w > width:
        image = cv2.resize(image, (width, max(1, int(h * width / w))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 60])
    return buf.tobytes() if ok else jpeg


class Uplink:
    def __init__(self, settings=None):
        self.settings = settings or cfg.cloud_settings()
        self._stop = threading.Event()
        self._wanted = set()
        self._frame_seq = {}
        self._last_warning = 0.0
        self.connected = False

    # ---- plumbing
    def session(self):
        s = requests.Session()
        s.headers["Authorization"] = f"Bearer {self.settings['key']}"
        return s

    def url(self, path):
        return f"{self.settings['url']}{path}"

    def _warn(self, message):
        """Log a connection problem at most once a minute."""
        if time.time() - self._last_warning > 60:
            self._last_warning = time.time()
            log.warning("%s", message)

    @staticmethod
    def _check(response):
        status = response.status_code
        if status >= 500 or status in RETRY_STATUSES:
            raise UplinkError(f"cloud answered {status}", temporary=True)
        if status >= 400:
            raise UplinkError(f"cloud rejected the request ({status}): {response.text[:120]}", temporary=False)

    # ---- live numbers and pictures
    def heartbeat_once(self, session):
        cameras = {}
        for cam_id, worker in registry.workers.items():
            cameras[cam_id] = {**worker.metrics, "alerts": worker.active_alerts()}
        try:
            response = session.post(self.url("/api/ingest/heartbeat"),
                                    json={"site": self.settings["site"], "cameras": cameras}, timeout=(5, 10))
            self._check(response)
        except (requests.RequestException, UplinkError) as exc:
            self.connected = False
            self._warn(f"cloud not reachable: {exc}")
            return False
        if not self.connected:
            log.info("connected to the cloud server %s", self.settings["url"])
        self.connected = True
        self._wanted = set(response.json().get("frames", []))
        return True

    def send_frames_once(self, session):
        sent = 0
        for cam_id in list(self._wanted):
            worker = registry.workers.get(cam_id)
            if worker is None:
                continue
            jpeg, seq = worker.latest_jpeg()
            if seq == self._frame_seq.get(cam_id):
                continue                                   # nothing new since the last upload
            self._frame_seq[cam_id] = seq
            try:
                session.post(self.url(f"/api/ingest/frame/{cam_id}"),
                             data=shrink(jpeg, self.settings["frame_width"]),
                             headers={"Content-Type": "image/jpeg"}, timeout=(5, 10))
                sent += 1
            except requests.RequestException:
                break
        return sent

    # ---- alerts
    def _incident_ref(self, row):
        return f"{self.settings['site']}-{row['created_at']}-{row['id']}"   # unique even if ids restart

    def _send_incident(self, session, item):
        row = db.get_incident_for_upload(item["incident_id"])
        if not row:
            return                                          # deleted at the pool before it was sent
        media = footage.open_media(row["id"], "snapshot")
        snapshot = None
        if media:
            snapshot = media[1].read_bytes() if media[0] == "file" else media[1]
        meta = {"cam_id": row["cam_id"], "track_id": row["track_id"], "elapsed_sec": row["elapsed_sec"],
                "event": row["event"], "created_at": row["created_at"], "client_ref": self._incident_ref(row)}
        files = {"snapshot": ("snapshot.jpg", snapshot, "image/jpeg")} if snapshot else None
        response = session.post(self.url("/api/ingest/incident"), data={"meta": json.dumps(meta)},
                                files=files, timeout=(5, 30))
        self._check(response)
        db.map_set(row["id"], response.json()["id"])

    def _cloud_id(self, item):
        cloud_id = db.map_get(item["incident_id"])
        if cloud_id is None:
            if db.outbox_has("incident", item["incident_id"]):
                raise UplinkError("the incident itself has not been sent yet", temporary=True)
            raise UplinkError("the incident was never accepted by the cloud", temporary=False)
        return cloud_id

    def _send_video(self, session, item):
        cloud_id = self._cloud_id(item)
        media = footage.open_media(item["incident_id"], "video")
        if not media:
            return                                          # clip no longer exists: nothing to send
        if media[0] == "file":
            with open(media[1], "rb") as handle:
                response = session.post(self.url(f"/api/ingest/incident/{cloud_id}/video"), data=handle,
                                        headers={"Content-Type": "video/mp4"}, timeout=(5, 300))
        else:
            response = session.post(self.url(f"/api/ingest/incident/{cloud_id}/video"), data=media[1],
                                    headers={"Content-Type": "video/mp4"}, timeout=(5, 300))
        self._check(response)

    def _send_resolve(self, session, item):
        cloud_id = self._cloud_id(item)
        response = session.post(self.url(f"/api/ingest/incident/{cloud_id}/resolve"),
                                json={"resolved_at": db.now_str()}, timeout=(5, 15))
        self._check(response)

    def process_outbox_once(self, session):
        """Deliver the oldest queued item. -> 'idle' | 'sent' | 'retry' | 'dropped'"""
        item = db.outbox_next()
        if not item:
            return "idle"
        handler = {"incident": self._send_incident, "video": self._send_video,
                   "resolve": self._send_resolve}.get(item["kind"])
        try:
            if handler is None:
                raise UplinkError(f"unknown item {item['kind']}", temporary=False)
            handler(session, item)
        except UplinkError as exc:
            if exc.temporary:
                db.outbox_failed(item["id"])
                self._warn(f"will retry {item['kind']} for incident {item['incident_id']}: {exc}")
                return "retry"
            log.error("dropping %s for incident %s: %s", item["kind"], item["incident_id"], exc)
            db.outbox_done(item["id"])
            return "dropped"
        except requests.RequestException as exc:
            db.outbox_failed(item["id"])
            self._warn(f"will retry {item['kind']} for incident {item['incident_id']}: {exc}")
            return "retry"
        db.outbox_done(item["id"])
        return "sent"

    # ---- background threads
    def _heartbeat_loop(self):
        session = self.session()
        while not self._stop.is_set():
            self.heartbeat_once(session)
            self._stop.wait(HEARTBEAT_SEC)

    def _frames_loop(self):
        session = self.session()
        interval = 1.0 / max(0.5, self.settings["frame_fps"])
        while not self._stop.is_set():
            watched = bool(self._wanted)                   # nobody watching -> no pictures, no bandwidth
            if watched:
                try:
                    self.send_frames_once(session)
                except Exception:
                    log.exception("sending frames failed")
            self._stop.wait(interval if watched else 0.5)

    def _outbox_loop(self):
        session = self.session()
        failures = 0
        while not self._stop.is_set():
            try:
                result = self.process_outbox_once(session)
            except Exception:
                log.exception("uplink queue error")
                result = "retry"
            if result == "retry":
                failures += 1
                self._stop.wait(min(60, 2 ** min(failures, 6)))
            else:
                failures = 0
                self._stop.wait(1 if result == "idle" else 0)

    def start(self):
        for target, name in ((self._heartbeat_loop, "uplink-heartbeat"), (self._frames_loop, "uplink-frames"),
                             (self._outbox_loop, "uplink-queue")):
            threading.Thread(target=target, daemon=True, name=name).start()
        log.info("uploading to %s as '%s'", self.settings["url"], self.settings["site"])

    def stop(self):
        self._stop.set()


_instance = None


def start():
    """Start sending to the cloud if it is configured."""
    global _instance
    if _instance is None and enabled():
        if not cfg.cloud_settings()["url"].startswith("https://"):
            log.warning("the cloud address is not https:// - the secret key would travel unencrypted")
        _instance = Uplink()
        _instance.start()
    return _instance
