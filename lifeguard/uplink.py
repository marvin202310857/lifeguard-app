"""Reliable pool-computer to cloud synchronization.

Heartbeats and small processed JPEG frames are sent to the Vercel API.
Incident snapshots and videos are uploaded directly to Supabase Storage, so
large media never passes through Vercel's function request-body limit. The
local outbox remains the source of retry order for incident events.
"""

import json
import logging
import os
import re
import threading
import time

import requests

from . import db, footage, registry, settings as cfg, storage

log = logging.getLogger("lifeguard.uplink")

HEARTBEAT_SEC = 2
RETRY_STATUSES = {401, 403, 408, 425, 429}
ZERO_METRICS = {"people_in_pool": 0, "people_outside_pool": 0, "submerged_count": 0}


class UplinkError(Exception):
    def __init__(self, message, temporary):
        super().__init__(message)
        self.temporary = temporary


def _settings():
    """Return validated edge-to-cloud settings, or None if disabled."""
    try:
        raw = cfg.cloud_settings()
    except Exception:
        log.exception("Could not load cloud uplink settings")
        raw = None

    if not isinstance(raw, dict):
        raw = {}

    try:
        config = cfg.load_config()
    except Exception:
        log.exception("Could not load LifeGuard configuration")
        config = {}

    cloud = config.get("cloud", {}) if isinstance(config, dict) else {}
    if not isinstance(cloud, dict):
        cloud = {}

    def first(*values):
        for value in values:
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    url = first(
        os.environ.get("LIFEGUARD_CLOUD_URL"),
        os.environ.get("LIFEGUARD_REMOTE_URL"),
        cloud.get("url"),
        raw.get("url"),
        config.get("remote_url") if isinstance(config, dict) else None,
    )
    key = first(
        os.environ.get("LIFEGUARD_CLOUD_KEY"),
        os.environ.get("LIFEGUARD_INGEST_KEY"),
        cloud.get("key"),
        cloud.get("ingest_key"),
        raw.get("key"),
        config.get("ingest_key") if isinstance(config, dict) else None,
        config.get("remote_key") if isinstance(config, dict) else None,
    )
    site = first(
        os.environ.get("LIFEGUARD_SITE"),
        cloud.get("site"),
        cloud.get("site_id"),
        raw.get("site"),
        config.get("site") if isinstance(config, dict) else None,
        config.get("site_name") if isinstance(config, dict) else None,
        config.get("site_id") if isinstance(config, dict) else None,
    )

    if not url or not key or not site:
        return None

    url = url.rstrip("/")
    if not (
        url.startswith("https://")
        or url.startswith("http://localhost:")
        or url.startswith("http://127.0.0.1:")
    ):
        log.warning("Cloud uplink URL must use HTTPS except for localhost; uplink disabled")
        return None

    try:
        frame_width = int(cloud.get("frame_width", raw.get("frame_width", 640)))
        frame_fps = float(cloud.get("frame_fps", raw.get("frame_fps", 2.0)))
    except (TypeError, ValueError):
        log.warning("Invalid cloud frame_width/frame_fps; using defaults")
        frame_width, frame_fps = 640, 2.0

    return {
        "url": url,
        "key": key,
        "site": site,
        "frame_width": max(160, min(frame_width, 1920)),
        "frame_fps": max(0.5, min(frame_fps, 5.0)),
    }


def enabled():
    return _settings() is not None


def enqueue(kind, incident_id):
    """Queue an incident, video, or resolve event for cloud delivery."""
    if not enabled():
        return
    try:
        db.outbox_add(kind, incident_id)
    except Exception:
        log.exception("Could not queue %s for incident %s", kind, incident_id)


def shrink(jpeg, width):
    """Create a lower-bandwidth JPEG copy for live cloud viewing."""
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return jpeg
    height, current_width = image.shape[:2]
    if current_width > width:
        image = cv2.resize(
            image,
            (width, max(1, int(height * width / current_width))),
            interpolation=cv2.INTER_AREA,
        )
    ok, buffer = cv2.imencode(
        ".jpg",
        image,
        [cv2.IMWRITE_JPEG_QUALITY, 60],
    )
    return buffer.tobytes() if ok else jpeg


def _safe_path_part(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "item"))[:100].strip(".-_") or "item"


def _camera_state(worker):
    """Metrics for the cloud: zeroed and marked offline unless frames are really arriving."""
    snap = getattr(worker, "snapshot", None)  # provided by the worker.py patch
    if callable(snap):
        try:
            state = dict(snap())
        except Exception:
            log.exception("Could not read camera snapshot")
            state = {"online": False}
    else:  # fallback if worker.py isn't patched yet
        state = dict(worker.metrics or {})
        state["online"] = state.get("status") != "OFFLINE"
    if not state.get("online"):
        state.update(ZERO_METRICS)
        state["status"] = "OFFLINE"
        state["online"] = False
    return state


class Uplink:
    def __init__(self, settings=None):
        self.settings = settings or _settings()
        if not isinstance(self.settings, dict):
            raise ValueError("Cloud uplink is not configured; check URL, key, and site.")
        missing = [name for name in ("url", "key", "site") if not self.settings.get(name)]
        if missing:
            raise ValueError("Cloud uplink settings missing: " + ", ".join(missing))

        self.settings.setdefault("frame_width", 640)
        self.settings.setdefault("frame_fps", 2.0)
        self._stop = threading.Event()
        self._wanted = set()
        self._last_logged_wanted = None
        self._frame_seq = {}
        self._last_warning = 0.0
        self.connected = False

    # Plumbing ----------------------------------------------------------------
    def session(self):
        session = requests.Session()
        session.headers["Authorization"] = f"Bearer {self.settings['key']}"
        bypass = os.environ.get("LIFEGUARD_VERCEL_BYPASS")
        if bypass:
            session.headers["x-vercel-protection-bypass"] = bypass
        return session

    def url(self, path):
        return f"{self.settings['url']}{path}"

    def _warn(self, message):
        if time.time() - self._last_warning > 60:
            self._last_warning = time.time()
            log.warning("%s", message)

    @staticmethod
    def _check(response):
        status = response.status_code
        if status >= 500 or status in RETRY_STATUSES:
            text = response.text or ""
            hint = ""
            if "Authentication Required" in text or "vercel.com/sso" in text:
                hint = " (Vercel Deployment Protection)"
            raise UplinkError(
                f"cloud answered {status}{hint}: {text[:80]!r}",
                temporary=True,
            )
        if status >= 400:
            raise UplinkError(
                f"cloud rejected request ({status}): {response.text[:160]}",
                temporary=False,
            )

    # Live metrics and processed frames --------------------------------------
    def heartbeat_once(self, session):
        cameras = {}
        for cam_id, worker in list(registry.workers.items()):
            try:
                alerts = worker.active_alerts()
            except Exception:
                alerts = []
            cameras[cam_id] = {**_camera_state(worker), "alerts": alerts}

        try:
            response = session.post(
                self.url("/api/ingest/heartbeat"),
                json={"site": self.settings["site"], "cameras": cameras},
                timeout=(5, 10),
            )
            self._check(response)
            payload = response.json()
            if not isinstance(payload, dict):
                raise UplinkError("cloud returned an invalid heartbeat response", True)
        except (requests.RequestException, ValueError, UplinkError) as exc:
            self.connected = False
            self._warn(f"cloud not reachable: {exc}")
            return False

        if not self.connected:
            log.info("Connected to cloud server %s", self.settings["url"])
        self.connected = True
        self._wanted = {
            cam_id for cam_id in payload.get("frames", [])
            if cam_id in registry.workers
        }

        # Debug: logged only when the list changes, so it doesn't spam every 2 s.
        current = sorted(self._wanted)
        if current != self._last_logged_wanted:
            self._last_logged_wanted = current
            log.info("cloud wants frames for: %s", current)
        return True

    def send_frames_once(self, session):
        sent = 0
        for cam_id in list(self._wanted):
            worker = registry.workers.get(cam_id)
            if worker is None:
                continue

            # Never upload the "OFFLINE" placeholder image as if it were live video.
            is_online = getattr(worker, "is_online", None)
            if callable(is_online) and not is_online():
                continue

            jpeg, sequence = worker.latest_jpeg()
            if not jpeg or sequence == self._frame_seq.get(cam_id):
                continue

            frame = shrink(jpeg, self.settings["frame_width"])
            if len(frame) > 900_000:
                self._warn(f"Frame for {cam_id} exceeds the 900 KB upload limit; skipping it")
                continue

            try:
                response = session.post(
                    self.url(f"/api/ingest/frame/{cam_id}"),
                    data=frame,
                    headers={"Content-Type": "image/jpeg"},
                    timeout=(5, 10),
                )
                self._check(response)
                self._frame_seq[cam_id] = sequence
                sent += 1
            except (requests.RequestException, UplinkError) as exc:
                self._warn(f"Frame upload failed for {cam_id}: {exc}")
                break
        return sent

    # Incident media ----------------------------------------------------------
    def _incident_ref(self, row):
        # New local incidents persist client_ref before enqueueing, allowing
        # the cloud API to update the same Supabase row instead of duplicating it.
        try:
            full_row = db.get_incident(row["id"])
            if full_row and full_row.get("client_ref"):
                return str(full_row["client_ref"])
        except Exception:
            log.exception("Could not read incident client_ref for %s", row.get("id"))
        # Compatibility for incidents created before client_ref was added.
        return f"{self.settings['site']}-{row['created_at']}-{row['id']}"

    def _snapshot_to_storage(self, row, client_ref):
        media = footage.open_media(row["id"], "snapshot")
        if not media:
            return None

        source, data = media[0], media[1]
        if source == "file":
            snapshot = data.read_bytes()
        elif source == "blob":
            snapshot = bytes(data)
        elif source == "cloud":
            snapshot = storage.download_bytes(data)
        else:
            return None

        if not snapshot:
            return None

        cam_id = _safe_path_part(row["cam_id"])
        ref_part = _safe_path_part(client_ref)
        destination = f"incidents/{cam_id}/edge/{ref_part}/snapshot.jpg"
        return storage.upload_bytes(destination, snapshot, "image/jpeg", upsert=True)

    def _send_incident(self, session, item):
        row = db.get_incident_for_upload(item["incident_id"])
        if not row:
            return

        client_ref = self._incident_ref(row)
        snapshot_path = self._snapshot_to_storage(row, client_ref)
        meta = {
            "cam_id": row["cam_id"],
            "track_id": row.get("track_id"),
            "elapsed_sec": row.get("elapsed_sec", 0),
            "event": row.get("event", "Drowning Risk Detected"),
            "created_at": row["created_at"],
            "client_ref": client_ref,
            "snapshot_path": snapshot_path,
            "alert_level": "DANGER",
        }

        response = session.post(
            self.url("/api/ingest/incident"),
            json={"site": self.settings["site"], "meta": meta},
            timeout=(5, 30),
        )
        self._check(response)
        cloud_id = response.json().get("id")
        if cloud_id is None:
            raise UplinkError("cloud did not return an incident ID", temporary=True)
        db.map_set(row["id"], cloud_id)

    def _cloud_id(self, item):
        cloud_id = db.map_get(item["incident_id"])
        if cloud_id is None:
            if db.outbox_has("incident", item["incident_id"]):
                raise UplinkError("incident has not been sent yet", temporary=True)
            raise UplinkError("incident was never accepted by the cloud", temporary=False)
        return cloud_id

    def _send_video(self, session, item):
        cloud_id = self._cloud_id(item)
        media = footage.open_media(item["incident_id"], "video")
        if not media:
            return

        source, data, mime = media
        incident = db.get_incident(item["incident_id"])
        if not incident:
            raise UplinkError("local incident no longer exists", temporary=False)

        cam_id = _safe_path_part(incident["cam_id"])
        destination = f"incidents/{cam_id}/cloud-{int(cloud_id)}/video.mp4"

        # Upload the video directly to Supabase. Do not send clip bytes through
        # Vercel Functions, which enforce a 4.5 MB request-body limit.
        if source == "file":
            storage.upload_file(data, destination, mime or "video/mp4", upsert=True)
        elif source == "blob":
            storage.upload_bytes(destination, bytes(data), mime or "video/mp4", upsert=True)
        elif source == "cloud":
            # Already stored in the shared bucket; reuse the existing object path.
            destination = data
        else:
            raise UplinkError(f"unsupported video media source: {source}", temporary=False)

        response = session.post(
            self.url(f"/api/ingest/incident/{cloud_id}/video"),
            json={
                "site": self.settings["site"],
                "storage_path": destination,
                "mime": mime or "video/mp4",
            },
            timeout=(5, 30),
        )
        self._check(response)

    def _send_resolve(self, session, item):
        cloud_id = self._cloud_id(item)
        response = session.post(
            self.url(f"/api/ingest/incident/{cloud_id}/resolve"),
            json={
                "site": self.settings["site"],
                "resolved_at": db.now_str(),
            },
            timeout=(5, 15),
        )
        self._check(response)

    def process_outbox_once(self, session):
        """Deliver the oldest item: idle, sent, retry, or dropped."""
        item = db.outbox_next()
        if not item:
            return "idle"

        handler = {
            "incident": self._send_incident,
            "video": self._send_video,
            "resolve": self._send_resolve,
        }.get(item["kind"])

        try:
            if handler is None:
                raise UplinkError(f"unknown outbox item kind: {item['kind']}", False)
            handler(session, item)
        except UplinkError as exc:
            if exc.temporary:
                db.outbox_failed(item["id"])
                self._warn(f"Will retry {item['kind']} for incident {item['incident_id']}: {exc}")
                return "retry"
            log.error("Dropping %s for incident %s: %s", item["kind"], item["incident_id"], exc)
            db.outbox_done(item["id"])
            return "dropped"
        except requests.RequestException as exc:
            db.outbox_failed(item["id"])
            self._warn(f"Will retry {item['kind']} for incident {item['incident_id']}: {exc}")
            return "retry"
        except Exception as exc:
            db.outbox_failed(item["id"])
            self._warn(f"Will retry {item['kind']} for incident {item['incident_id']}: {exc}")
            return "retry"

        db.outbox_done(item["id"])
        return "sent"

    # Background loops --------------------------------------------------------
    def _heartbeat_loop(self):
        session = self.session()
        while not self._stop.is_set():
            self.heartbeat_once(session)
            self._stop.wait(HEARTBEAT_SEC)

    def _frames_loop(self):
        session = self.session()
        interval = 1.0 / max(0.5, self.settings["frame_fps"])
        while not self._stop.is_set():
            watched = bool(self._wanted)
            if watched:
                try:
                    self.send_frames_once(session)
                except Exception:
                    log.exception("Sending live frames failed")
            self._stop.wait(interval if watched else 0.5)

    def _outbox_loop(self):
        session = self.session()
        failures = 0
        while not self._stop.is_set():
            try:
                result = self.process_outbox_once(session)
            except Exception:
                log.exception("Uplink queue error")
                result = "retry"

            if result == "retry":
                failures += 1
                self._stop.wait(min(60, 2 ** min(failures, 6)))
            else:
                failures = 0
                self._stop.wait(1 if result == "idle" else 0)

    def start(self):
        targets = (
            (self._heartbeat_loop, "uplink-heartbeat"),
            (self._frames_loop, "uplink-frames"),
            (self._outbox_loop, "uplink-queue"),
        )
        for target, name in targets:
            threading.Thread(target=target, daemon=True, name=name).start()
        log.info("Uploading LifeGuard data to %s as '%s'", self.settings["url"], self.settings["site"])

    def stop(self):
        self._stop.set()


_instance = None
_instance_lock = threading.Lock()


def start():
    """Start the cloud uploader once when valid settings are present."""
    global _instance
    with _instance_lock:
        if _instance is not None:
            return _instance
        settings = _settings()
        if settings is None:
            log.info("Cloud uplink disabled: configure URL, key, and site.")
            return None
        _instance = Uplink(settings)
        _instance.start()
        return _instance