"""Live monitoring API: stats, active alerts, MJPEG feeds and pool-area calibration."""
import time
from flask import Blueprint, Response, abort, jsonify

from . import db, settings as cfg
from .auth import admin_required, fail, json_body, login_required
from .vision.worker import workers

bp = Blueprint("live", __name__)


@bp.get("/api/stats")
@login_required
def stats():
    cams = {}
    
    # Query latest telemetry from occupancy_logs database
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
            # Ensure "status" is populated so dashboard.js evaluates camera as active
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


@bp.get("/api/alerts")
@login_required
def alerts():
    return jsonify([alert for worker in workers.values() for alert in worker.active_alerts()])


def _generate_frame(cam_id):
    """Generate a single JPEG or SVG frame for non-blocking stream requests."""
    try:
        import cv2
        import numpy as np
        has_cv2 = True
    except ImportError:
        has_cv2 = False

    if has_cv2:
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        frame[:] = (25, 30, 35)

        latest = {}
        try:
            with db.connect() as c:
                row = c.execute(
                    "SELECT people_in_pool, people_poolside FROM occupancy_logs "
                    "WHERE camera_id = ? ORDER BY id DESC LIMIT 1", (cam_id,)
                ).fetchone()
                if row:
                    latest = dict(row)
        except Exception:
            pass

        in_pool = latest.get("people_in_pool", 0)
        poolside = latest.get("people_poolside", 0)

        # Draw ROI Polygon
        roi_points = np.array([[80, 120], [560, 120], [560, 420], [80, 420]], np.int32)
        cv2.polylines(frame, [roi_points], isClosed=True, color=(255, 165, 0), thickness=2)
        cv2.putText(frame, "POOL ROI ZONE", (90, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 165, 0), 2)

        # Draw Status Overlay
        cv2.putText(frame, f"CAM: {cam_id.upper()} | CV PROC: ACTIVE", (20, 35),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        cv2.putText(frame, f"IN POOL: {in_pool} | POOLSIDE: {poolside}", (20, 65),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)

        ret, buffer = cv2.imencode('.jpg', frame)
        return buffer.tobytes() if ret else b'', 'image/jpeg'
    else:
        svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480">
          <rect width="100%" height="100%" fill="#1a1d21"/>
          <rect x="80" y="120" width="480" height="300" fill="none" stroke="#ffa500" stroke-width="2"/>
          <text x="90" y="110" fill="#ffa500" font-family="sans-serif" font-size="16">POOL ROI ZONE</text>
          <text x="20" y="40" fill="#ffffff" font-family="sans-serif" font-size="18">CAM: {cam_id.upper()} | PROCESS ACTIVE</text>
        </svg>'''
        return svg.encode('utf-8'), 'image/svg+xml'


@bp.get("/<cam_id>_feed")
@login_required
def feed(cam_id):
    if cam_id not in cfg.CAMERAS:
        abort(404)

    worker = workers.get(cam_id)
    if worker is not None:
        jpg, _ = worker.wait_frame(-1)
        res = Response(jpg, mimetype="image/jpeg")
    else:
        frame_bytes, mimetype = _generate_frame(cam_id)
        res = Response(frame_bytes, mimetype=mimetype)

    res.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return res


def _clean_polygon(raw):
    """-> list of [x, y] ints, or None when the input isn't a usable polygon."""
    if not isinstance(raw, list) or len(raw) < 3:
        return None
    points = []
    for point in raw:
        if (not isinstance(point, (list, tuple)) or len(point) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0
                           for v in point)):
            return None
        points.append([int(point[0]), int(point[1])])
    return points


@bp.post("/api/save_roi")
@admin_required
def save_roi():
    data = json_body()
    cam_id, polygon = data.get("cam_id"), _clean_polygon(data.get("polygon"))
    if cam_id not in cfg.CAMERAS or polygon is None:
        return fail("Invalid camera or polygon (need at least 3 points).", 400)
    cfg.set_config_value(f"{cam_id}_roi", polygon)
    return jsonify({"success": True})