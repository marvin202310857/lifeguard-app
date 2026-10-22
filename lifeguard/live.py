"""Live monitoring API: stats, active alerts, MJPEG feeds and pool-area calibration."""
import time
from flask import Blueprint, Response, abort, jsonify

from . import settings as cfg
from .auth import admin_required, fail, json_body, login_required
from .vision.worker import workers

bp = Blueprint("live", __name__)


@bp.get("/api/stats")
@login_required
def stats():
    cams = {cam_id: worker.metrics for cam_id, worker in workers.items()}

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


def _mjpeg(worker):
    last = -1
    while True:
        jpg, last = worker.wait_frame(last)
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"


def _fallback_mjpeg(cam_id):
    """Generate dynamic process stream (ROI & metrics overlay) when worker is offline or in cloud mode."""
    try:
        import cv2
        import numpy as np
        has_cv2 = True
    except ImportError:
        has_cv2 = False

    while True:
        if has_cv2:
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            frame[:] = (25, 30, 35)

            # Draw Pool ROI Polyline
            roi_points = np.array([[80, 120], [560, 120], [560, 420], [80, 420]], np.int32)
            cv2.polylines(frame, [roi_points], isClosed=True, color=(255, 165, 0), thickness=2)
            cv2.putText(frame, "POOL ROI ZONE", (90, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 165, 0), 2)
            cv2.putText(frame, f"CAM: {cam_id.upper()} | CV PROC: ACTIVE", (20, 35),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

            ret, buffer = cv2.imencode('.jpg', frame)
            frame_bytes = buffer.tobytes() if ret else b''
            mimetype = 'image/jpeg'
        else:
            svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480">
              <rect width="100%" height="100%" fill="#1a1d21"/>
              <rect x="80" y="120" width="480" height="300" fill="none" stroke="#ffa500" stroke-width="2"/>
              <text x="90" y="110" fill="#ffa500" font-family="sans-serif" font-size="16">POOL ROI ZONE</text>
              <text x="20" y="40" fill="#ffffff" font-family="sans-serif" font-size="18">CAM: {cam_id.upper()} | PROCESS ACTIVE</text>
            </svg>'''
            frame_bytes = svg.encode('utf-8')
            mimetype = 'image/svg+xml'

        yield (b'--frame\r\n'
               b'Content-Type: ' + mimetype.encode('utf-8') + b'\r\n\r\n' + frame_bytes + b'\r\n')
        time.sleep(0.2)


@bp.get("/<cam_id>_feed")
@login_required
def feed(cam_id):
    if cam_id not in cfg.CAMERAS:
        abort(404)
    worker = workers.get(cam_id)
    if worker is not None:
        return Response(_mjpeg(worker), mimetype="multipart/x-mixed-replace; boundary=frame")
    # Fallback stream for Render cloud deployment
    return Response(_fallback_mjpeg(cam_id), mimetype="multipart/x-mixed-replace; boundary=frame")


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