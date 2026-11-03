"""Live monitoring API for local workers and Vercel's shared camera state."""

import html
import logging
import time

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import numpy as np
except ImportError:
    np = None

from flask import Blueprint, Response, abort, jsonify

from . import db, settings as cfg, storage
from .registry import workers

log = logging.getLogger("lifeguard.live")
bp = Blueprint("live", __name__)

HEARTBEAT_STALE_SEC = 10
NO_STORE = "no-store, no-cache, must-revalidate, max-age=0"


def _no_store(response):
    response.headers["Cache-Control"] = NO_STORE
    response.headers["Pragma"] = "no-cache"
    return response


def _offline_metrics(last_updated=None):
    return {
        "online": False,
        "status": "OFFLINE",
        "people_in_pool": 0,
        "people_outside_pool": 0,
        "submerged_count": 0,
        "last_updated": last_updated,
    }


def _cloud_metrics(cam_id):
    state = storage.read_live_state(cam_id)
    if not state:
        return _offline_metrics()

    try:
        age = time.time() - float(state.get("received_at", 0))
    except (TypeError, ValueError):
        age = float("inf")

    report = state.get("report", {})
    if not isinstance(report, dict):
        report = {}

    last_updated = report.get("last_updated")
    if age < 0 or age > HEARTBEAT_STALE_SEC:
        return _offline_metrics(last_updated)

    status = str(report.get("status") or "OFFLINE").upper()
    if status not in {"SAFE", "WARNING", "DANGER", "OFFLINE"}:
        status = "OFFLINE"

    def count(key):
        try:
            return max(0, int(report.get(key, 0) or 0))
        except (TypeError, ValueError):
            return 0

    if status == "OFFLINE":
        return _offline_metrics(last_updated)

    return {
        "online": True,
        "status": status,
        "people_in_pool": count("people_in_pool"),
        "people_outside_pool": count("people_outside_pool"),
        "submerged_count": count("submerged_count"),
        "last_updated": last_updated,
    }


def _local_metrics(cam_id):
    worker = workers.get(cam_id)
    if worker is None:
        return None

    # Preferred: CameraWorker.snapshot() derives "online" from the last frame time,
    # so a hung or dead worker can never keep showing old numbers.
    snap = getattr(worker, "snapshot", None)
    if callable(snap):
        try:
            data = dict(snap())
        except Exception:
            log.exception("[%s] Could not read worker snapshot", cam_id)
            return _offline_metrics()
        online = bool(data.get("online"))
        if not online:
            return _offline_metrics(data.get("last_updated"))
        return {
            "online": True,
            "status": str(data.get("status") or "SAFE").upper(),
            "people_in_pool": int(data.get("people_in_pool") or 0),
            "people_outside_pool": int(data.get("people_outside_pool") or 0),
            "submerged_count": int(data.get("submerged_count") or 0),
            "last_updated": data.get("last_updated"),
        }

    # Fallback for a worker.py that has not been patched yet.
    try:
        metrics = dict(worker.metrics or {})
    except Exception:
        log.exception("[%s] Could not read worker metrics", cam_id)
        return _offline_metrics()

    is_alive = getattr(worker, "is_alive", None)
    online = bool(is_alive()) if callable(is_alive) else True
    status = str(metrics.get("status") or "OFFLINE").upper()
    if not online or status == "OFFLINE":
        return _offline_metrics(metrics.get("last_updated"))

    metrics["status"] = status
    metrics["online"] = True
    return metrics


def _camera_metrics(cam_id):
    if cfg.is_cloud():
        return _cloud_metrics(cam_id)
    metrics = _local_metrics(cam_id)
    return metrics if metrics is not None else _offline_metrics()


@bp.get("/api/stats")
def stats():
    """Return independent metrics for each configured camera."""
    cameras = {
        cam_id: _camera_metrics(cam_id)
        for cam_id in cfg.CAMERAS
    }

    def total(key):
        return sum(int(item.get(key, 0) or 0) for item in cameras.values())

    result = {
        "cameras": cameras,
        "online_count": sum(1 for item in cameras.values() if item.get("online")),
        "total_cameras": len(cameras),
        "total_in_pool": total("people_in_pool"),
        "total_outside": total("people_outside_pool"),
        "total_submerged": total("submerged_count"),
        "server_time": time.time(),
    }
    # Keep the original dashboard's stats.cam1 / stats.cam2 shape.
    result.update(cameras)
    return _no_store(jsonify(result))


def _standby_response(cam_id, reason):
    """Offline placeholder. Works on Vercel even when OpenCV is not installed."""
    response = None

    if cv2 is not None and np is not None:
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        cv2.putText(
            frame,
            f"CAM: {cam_id.upper()} | OFFLINE",
            (40, 240),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 215, 255),
            2,
        )
        ok, encoded = cv2.imencode(".jpg", frame)
        if ok:
            response = Response(encoded.tobytes(), mimetype="image/jpeg")

    if response is None:
        label = html.escape(cam_id.upper())
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480" '
            'viewBox="0 0 640 480">'
            '<rect width="640" height="480" fill="#0b1220"/>'
            '<text x="320" y="240" fill="#ffd700" font-family="Arial, sans-serif" '
            'font-size="28" text-anchor="middle">'
            f"CAM {label} | OFFLINE</text></svg>"
        )
        response = Response(svg, mimetype="image/svg+xml")

    response.headers["X-LifeGuard-Feed"] = "offline"
    response.headers["X-LifeGuard-Reason"] = reason
    return _no_store(response)


def _jpeg_response(jpg):
    response = Response(jpg, mimetype="image/jpeg")
    response.headers["X-LifeGuard-Feed"] = "live"
    return _no_store(response)


@bp.get("/<cam_id>_feed")
def feed(cam_id):
    """Return the processed JPEG for a camera and renew its viewer lease.

    When no live frame can be served, an explicit OFFLINE placeholder is returned.
    Check the X-LifeGuard-Reason response header (browser DevTools > Network) to see why.
    """
    if cam_id not in cfg.CAMERAS:
        abort(404)

    if cfg.is_cloud():
        reason = "camera-offline-or-stale-heartbeat"

        # Renewing the lease is what tells the pool computer to start uploading frames.
        try:
            storage.write_viewer_lease(cam_id)
        except Exception:
            log.exception("[%s] Could not write viewer lease", cam_id)
            reason = "viewer-lease-write-failed"

        try:
            metrics = _cloud_metrics(cam_id)
            if metrics.get("online"):
                jpg = storage.read_live_frame(cam_id)
                if jpg:
                    return _jpeg_response(jpg)
                reason = "online-but-no-frame-uploaded-yet"
        except Exception:
            log.exception("[%s] Could not retrieve shared cloud frame", cam_id)
            reason = "storage-read-failed"

        return _standby_response(cam_id, reason)

    worker = workers.get(cam_id)
    if worker is not None:
        is_online = getattr(worker, "is_online", None)
        if not callable(is_online) or is_online():
            latest = getattr(worker, "latest_jpeg", None)
            if callable(latest):
                try:
                    jpg, _sequence = latest()
                    if jpg:
                        return _jpeg_response(jpg)
                except Exception:
                    log.exception("[%s] Could not retrieve local processed frame", cam_id)

    return _standby_response(cam_id, "local-camera-offline")


@bp.get("/api/live-debug")
def live_debug():
    """Temporary diagnostic (cloud only): shows what the Vercel side can see in Storage.

    Open /api/live-debug while the dashboard is open and run.py is running.
    Remove this route once live video works.
    """
    if not cfg.is_cloud():
        abort(404)

    result = {"site_id_used_by_cloud": None, "cameras": {}, "server_time": time.time()}
    try:
        result["site_id_used_by_cloud"] = storage.site_id()
    except Exception as exc:
        result["site_error"] = str(exc)[:160]

    for cam_id in cfg.CAMERAS:
        info = {}
        try:
            state = storage.read_live_state(cam_id)
            if state:
                info["heartbeat_age_sec"] = round(time.time() - float(state.get("received_at", 0)), 1)
                info["reported_status"] = (state.get("report") or {}).get("status")
            else:
                info["heartbeat_age_sec"] = None
        except Exception as exc:
            info["state_error"] = str(exc)[:160]

        try:
            info["viewer_lease_active"] = storage.viewer_is_active(cam_id)
        except Exception as exc:
            info["lease_error"] = str(exc)[:160]

        try:
            base = storage._live_base()
            meta = storage._json_download(f"{base}/frame-state/{cam_id}.json")
            info["frame_age_sec"] = (
                round(time.time() - float(meta.get("frame_updated_at", 0)), 1) if meta else None
            )
        except Exception as exc:
            info["frame_error"] = str(exc)[:160]

        result["cameras"][cam_id] = info

    return _no_store(jsonify(result))