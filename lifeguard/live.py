"""Live monitoring API: stats, active alerts, MJPEG feeds and pool-area calibration."""
import time
import cv2
import numpy as np
from flask import Blueprint, Response, abort, jsonify

from . import db, settings as cfg
from .auth import admin_required, fail, json_body, login_required
from .ingest import LATEST_FRAMES
from .vision.worker import workers

bp = Blueprint("live", __name__)


@bp.get("/api/stats")
def stats():
    cams = {}
    latest_telemetry = {}
    try:
        with db.connect() as c:
            rows = c.execute(
                "SELECT camera_id, people_in_pool, people_poolside, submerged_count, timestamp "
                "FROM occupancy_logs ORDER BY id DESC LIMIT 10"
            ).fetchall()
            for r in rows:
                cam = r["camera_id"]
                if cam not in latest_telemetry:
                    latest_telemetry[cam] = dict(r)
    except Exception:
        pass

    for cam_id in cfg.CAMERAS:
        if cam_id in workers:
            cams[cam_id] = workers[cam_id].metrics
            cams[cam_id]["online"] = True
            cams[cam_id]["status"] = "SAFE"
        else:
            tel = latest_telemetry.get(cam_id, {})
            has_recent_frame = cam_id in LATEST_FRAMES
            is_online = has_recent_frame or bool(tel)
            
            submerged = tel.get("submerged_count", 0)
            if not is_online:
                cam_status = "OFFLINE"
            elif submerged > 0:
                cam_status = "DANGER"
            else:
                cam_status = "SAFE"

            cams[cam_id] = {
                "online": is_online,
                "status": cam_status,
                "people_in_pool": tel.get("people_in_pool", 0),
                "people_outside_pool": tel.get("people_poolside", 0),
                "submerged_count": submerged,
                "last_updated": tel.get("timestamp", "")
            }

    def total(key):
        return sum(m.get(key, 0) for m in cams.values())

    return jsonify({**cams,
                    "cameras": cams,
                    "cam1": cams.get("cam1", {}),
                    "cam2": cams.get("cam2", {}),
                    "total_in_pool": total("people_in_pool"),
                    "total_outside": total("people_outside_pool"),
                    "total_submerged": total("submerged_count")})


@bp.get("/<cam_id>_feed")
def feed(cam_id):
    """Return live frame directly without blocking HTTP streams."""
    if cam_id not in cfg.CAMERAS:
        abort(404)

    # 1. Check local vision worker
    worker = workers.get(cam_id)
    if worker is not None:
        jpg, _ = worker.wait_frame(-1)
        if jpg:
            res = Response(jpg, mimetype="image/jpeg")
            res.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            return res

    # 2. Check uploaded camera frame cache from test script / ESP32
    if cam_id in LATEST_FRAMES:
        res = Response(LATEST_FRAMES[cam_id], mimetype="image/jpeg")
        res.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return res

    # 3. Dynamic Standby Frame (Returns HTTP 200 instead of 404)
    standby = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.putText(standby, f"CAM: {cam_id.upper()} | WAITING FOR FEED...", (40, 240),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 215, 255), 2)
    _, buffer = cv2.imencode(".jpg", standby)
    
    res = Response(buffer.tobytes(), mimetype="image/jpeg")
    res.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return res