"""Live monitoring API: stats, active alerts, MJPEG feeds and pool-area calibration."""
from flask import Blueprint, Response, abort, jsonify

from . import settings as cfg
from .auth import admin_required, fail, json_body, login_required
from .registry import workers

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


@bp.get("/<cam_id>_feed")
@login_required
def feed(cam_id):
    worker = workers.get(cam_id)
    if worker is None:
        abort(404)
    return Response(_mjpeg(worker), mimetype="multipart/x-mixed-replace; boundary=frame")


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
