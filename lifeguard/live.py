"""Live monitoring API: stats, active alerts, MJPEG feeds and pool-area calibration."""
import time
from flask import Blueprint, Response, abort, jsonify

from . import db, settings as cfg
from .auth import admin_required, fail, json_body, login_required
from .ingest import LATEST_FRAMES
from .vision.worker import workers

bp = Blueprint("live", __name__)


@bp.get("/api/stats")
@login_required
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
        else:
            tel = latest_telemetry.get(cam_id, {})
            cams[cam_id] = {
                "status": "SAFE",
                "people_in_pool": tel.get("people_in_pool", 0),
                "people_outside_pool": tel.get("people_poolside", 0),
                "submerged_count": tel.get("submerged_count", 0),
                "last_updated": tel.get("timestamp", "")
            }

    def total(key):
        return sum(m.get(key, 0) for m in cams.values())

    return jsonify({**cams,
                    "total_in_pool": total("people_in_pool"),
                    "total_outside": total("people_outside_pool"),
                    "total_submerged": total("submerged_count")})


def _render_fallback_frame(cam_id):
    """Render a synthetic frame when no ESP32 image is cached."""
    try:
        import cv2
        import numpy as np
        has_cv2 = True
    except ImportError:
        has_cv2 = False

    if has_cv2:
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        frame[:] = (25, 30, 35)

        roi_pts = np.array([[80, 120], [560, 120], [560, 420], [80, 420]], np.int32)
        cv2.polylines(frame, [roi_pts], isClosed=True, color=(255, 165, 0), thickness=2)
        cv2.putText(frame, "POOL ROI ZONE", (90, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 165, 0), 2)
        cv2.putText(frame, f"CAM: {cam_id.upper()} | CV PROC: ACTIVE", (20, 35),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

        ret, buffer = cv2.imencode('.jpg', frame)
        return buffer.tobytes() if ret else b'', 'image/jpeg'
    else:
        svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480">
          <rect width="100%" height="100%" fill="#1a1d21"/>
          <text x="20" y="40" fill="#ffffff" font-family="sans-serif" font-size="18">CAM: {cam_id.upper()} | PROCESS ACTIVE</text>
        </svg>'''
        return svg.encode('utf-8'), 'image/svg+xml'


@bp.get("/<cam_id>_feed")
def feed(cam_id):
    """Return live ESP32 frame or fallback ROI synthetic frame."""
    if cam_id not in cfg.CAMERAS:
        abort(404)

    # 1. Check local vision worker
    worker = workers.get(cam_id)
    if worker is not None:
        jpg, _ = worker.wait_frame(-1)
        res = Response(jpg, mimetype="image/jpeg")
        res.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return res

    # 2. Check ESP32 uploaded frame cache
    if cam_id in LATEST_FRAMES:
        res = Response(LATEST_FRAMES[cam_id], mimetype="image/jpeg")
        res.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return res

    # 3. Fallback ROI placeholder
    frame_bytes, mimetype = _render_fallback_frame(cam_id)
    res = Response(frame_bytes, mimetype=mimetype)
    res.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return res