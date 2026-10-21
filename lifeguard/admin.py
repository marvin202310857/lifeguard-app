"""Administrator API: accounts, activity log, security alerts, incident footage, and occupancy telemetry."""
import time
from io import BytesIO

from flask import Blueprint, jsonify, request, send_file, redirect, Response

from . import db, footage, storage
from .auth import admin_required, client_ip, current_user, fail, json_body

bp = Blueprint("admin", __name__, url_prefix="/api")


# ----------------------------------------------------------------- video feed & ROI rendering
@bp.route("/<cam_id>_feed")
def live_feed(cam_id):
    """Dynamic stream displaying computer vision output: ROI polygons, bounding boxes, and detection metrics."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return "OpenCV required for computer vision processing output", 500

    def generate_frames():
        while True:
            # Create process output canvas (640x480 frame)
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            frame[:] = (25, 30, 35) # Dark gray container background

            # Fetch latest telemetry values from database
            latest = get_occupancy().get_json() or {}
            in_pool = latest.get("people_in_pool", 0)
            poolside = latest.get("people_poolside", 0)

            # 1. Draw Pool ROI (Region of Interest Boundary)
            roi_points = np.array([[80, 120], [560, 120], [560, 420], [80, 420]], np.int32)
            cv2.polylines(frame, [roi_points], isClosed=True, color=(255, 165, 0), thickness=2)
            cv2.putText(frame, "POOL ROI ZONE", (90, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 165, 0), 2)

            # 2. Draw Simulated Person Detection Boxes
            if in_pool > 0:
                cv2.rectangle(frame, (180, 180), (260, 340), (0, 255, 0), 2)
                cv2.putText(frame, "Person: In Pool", (180, 170), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
            if poolside > 0:
                cv2.rectangle(frame, (20, 200), (70, 320), (0, 215, 255), 2)
                cv2.putText(frame, "Person: Poolside", (10, 190), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 215, 255), 1)

            # 3. Draw On-Screen Process Summary
            cv2.putText(frame, f"CAM: {cam_id.upper()} | CV PROC: ACTIVE", (20, 35),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            cv2.putText(frame, f"IN POOL: {in_pool} | POOLSIDE: {poolside}", (20, 65),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)

            ret, buffer = cv2.imencode('.jpg', frame)
            if not ret:
                continue

            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
            time.sleep(0.1)  # ~10 FPS

    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')


# ----------------------------------------------------------------- telemetry & occupancy
@bp.post("/occupancy")
def receive_occupancy():
    """Public endpoint to receive telemetry & ROI metrics from ESP32 or local CV pipeline."""
    data = request.get_json(silent=True) or {}
    cam_id = str(data.get("camera_id", "cam1")).strip()
    in_pool = int(data.get("people_in_pool", 0))
    poolside = int(data.get("people_poolside", 0))
    submerged = int(data.get("submerged_count", 0))

    db.log_occupancy(cam_id, in_pool, poolside, submerged)
    return jsonify({"status": "ok", "camera_id": cam_id}), 200


@bp.get("/occupancy")
def get_occupancy():
    """Retrieve the latest telemetry and occupancy metrics for dashboard updates."""
    with db.connect() as c:
        row = c.execute(
            "SELECT camera_id, people_in_pool, people_poolside, submerged_count, timestamp "
            "FROM occupancy_logs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    if not row:
        return jsonify({
            "camera_id": "cam1",
            "people_in_pool": 0,
            "people_poolside": 0,
            "submerged_count": 0,
            "timestamp": db.now_str()
        })
    return jsonify(dict(row))


# ----------------------------------------------------------------- accounts
@bp.get("/users")
@admin_required
def users():
    return jsonify(db.list_users())


@bp.post("/users/approve")
@admin_required
def approve_user():
    user_id = json_body().get("userId")
    return jsonify({"success": bool(user_id) and db.approve_user(user_id) > 0})


@bp.post("/users/reject")
@admin_required
def reject_user():
    user_id = json_body().get("userId")
    if not user_id:
        return fail("Missing userId.", 400)
    if str(user_id) == current_user()["id"]:
        return fail("You can't remove your own account.", 400)
    ok, info = db.delete_user(user_id)
    return (jsonify({"success": True}), 200) if ok else fail(info, 400)


@bp.post("/users/unlock")
@admin_required
def unlock_user():
    username = str(json_body().get("username", "")).strip()
    if not username:
        return fail("Missing username.", 400)
    db.reset_throttle(username)
    db.log_event(username, "ACCOUNT_UNLOCKED", True, client_ip(), request.user_agent.string,
                 f"Unlocked by {current_user()['username']}")
    return jsonify({"success": True})


# ------------------------------------------------------- logs & security alerts
@bp.get("/login_logs")
@admin_required
def login_logs():
    limit = request.args.get("limit", default=100, type=int)
    return jsonify(db.get_login_logs(max(1, min(limit, 500))))


@bp.get("/security_alerts")
@admin_required
def security_alerts():
    return jsonify(db.get_admin_alerts(only_open=True))


@bp.post("/security_alerts/ack")
@admin_required
def ack_security_alert():
    alert_id = json_body().get("id")
    try:
        db.ack_admin_alert(alert_id)
    except (TypeError, ValueError):
        return fail("Invalid alert id.", 400)
    return jsonify({"success": True})


# ---------------------------------------------------------------- incidents
@bp.get("/incidents")
@admin_required
def incidents():
    return jsonify(db.list_incidents())


def _serve_media(incident_id, kind):
    media = footage.open_media(incident_id, kind)
    if not media:
        return fail(f"No {kind} for this incident.", 404)
        
    source, data, mime = media
    
    if source == "cloud":
        secure_url = storage.get_secure_video_url(data)
        if secure_url:
            return redirect(secure_url)
        return fail(f"Could not generate secure playback URL for {kind}.", 500)
        
    extension = "mp4" if kind == "video" else "jpg"
    return send_file(data if source == "file" else BytesIO(data), mimetype=mime, conditional=True,
                     download_name=f"incident_{incident_id}.{extension}",
                     as_attachment=request.args.get("download") == "1")


@bp.get("/incidents/<int:incident_id>/video")
@admin_required
def incident_video(incident_id):
    return _serve_media(incident_id, "video")


@bp.get("/incidents/<int:incident_id>/snapshot")
@admin_required
def incident_snapshot(incident_id):
    return _serve_media(incident_id, "snapshot")


@bp.delete("/incidents/<int:incident_id>")
@admin_required
def delete_incident(incident_id):
    return jsonify({"success": footage.delete_incident(incident_id)})


# ------------------------------------------------------------------ footage
@bp.get("/footage")
@admin_required
def footage_overview():
    return jsonify(footage.folder_stats())


@bp.get("/footage/<day>")
@admin_required
def footage_day(day):
    if not footage.is_valid_day(day):
        return fail("Invalid date.", 400)
    return jsonify(db.list_incidents_for_day(day))