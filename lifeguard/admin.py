"""Administrator API: accounts, activity log, security alerts,
incident footage, and occupancy telemetry.
"""

import time
from io import BytesIO

from flask import (
    Blueprint,
    jsonify,
    request,
    send_file,
    redirect,
    Response,
)

from . import db, footage, storage
from .auth import (
    admin_required,
    client_ip,
    current_user,
    fail,
    json_body,
)

bp = Blueprint("admin", __name__, url_prefix="/api")


# ============================================================
# LIVE FEED AND ROI DISPLAY
# ============================================================

def generate_mjpeg_stream(cam_id):
    """Generate the dashboard's telemetry/ROI demonstration stream."""

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

            try:
                latest = get_occupancy().get_json() or {}
            except Exception:
                latest = {}

            if latest.get("camera_id") == cam_id:
                in_pool = int(latest.get("people_in_pool") or 0)
                poolside = int(latest.get("people_poolside") or 0)
            else:
                in_pool = 0
                poolside = 0

            roi_points = np.array(
                [[80, 120], [560, 120], [560, 420], [80, 420]],
                np.int32,
            )

            cv2.polylines(
                frame,
                [roi_points],
                isClosed=True,
                color=(255, 165, 0),
                thickness=2,
            )

            cv2.putText(
                frame,
                "POOL ROI ZONE",
                (90, 110),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (255, 165, 0),
                2,
            )

            if in_pool > 0:
                cv2.rectangle(
                    frame,
                    (180, 180),
                    (260, 340),
                    (0, 255, 0),
                    2,
                )

                cv2.putText(
                    frame,
                    "Person: In Pool",
                    (180, 170),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    (0, 255, 0),
                    2,
                )

            if poolside > 0:
                cv2.rectangle(
                    frame,
                    (20, 200),
                    (70, 320),
                    (0, 215, 255),
                    2,
                )

                cv2.putText(
                    frame,
                    "Person: Poolside",
                    (10, 190),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.4,
                    (0, 215, 255),
                    1,
                )

            cv2.putText(
                frame,
                f"CAM: {cam_id.upper()} | CV PROC: ACTIVE",
                (20, 35),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (255, 255, 255),
                2,
            )

            cv2.putText(
                frame,
                f"IN POOL: {in_pool} | POOLSIDE: {poolside}",
                (20, 65),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (200, 200, 200),
                1,
            )

            ret, buffer = cv2.imencode(".jpg", frame)
            frame_bytes = buffer.tobytes() if ret else b""
            content_type = "image/jpeg"

        else:
            svg = f"""<svg xmlns="http://www.w3.org/2000/svg"
                width="640" height="480">
                <rect width="100%" height="100%" fill="#1a1d21"/>
                <rect x="80" y="120" width="480" height="300"
                    fill="none" stroke="#ffa500" stroke-width="2"/>
                <text x="90" y="110" fill="#ffa500"
                    font-family="sans-serif" font-size="16">
                    POOL ROI ZONE
                </text>
                <text x="20" y="40" fill="#ffffff"
                    font-family="sans-serif" font-size="18">
                    CAM: {cam_id.upper()} | PROCESS ACTIVE
                </text>
            </svg>"""

            frame_bytes = svg.encode("utf-8")
            content_type = "image/svg+xml"

        yield (
            b"--frame\r\n"
            b"Content-Type: "
            + content_type.encode("utf-8")
            + b"\r\n\r\n"
            + frame_bytes
            + b"\r\n"
        )

        time.sleep(0.2)


@bp.route("/<cam_id>_feed")
def live_feed_api(cam_id):
    return Response(
        generate_mjpeg_stream(cam_id),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


# ============================================================
# OCCUPANCY AND TELEMETRY
# ============================================================

@bp.post("/occupancy")
def receive_occupancy():
    """Receive occupancy readings from the ESP32 or local CV pipeline."""

    data = request.get_json(silent=True) or {}

    cam_id = str(data.get("camera_id") or "cam1").strip()

    if not cam_id:
        return fail("Missing camera ID.", 400)

    try:
        in_pool = max(0, int(data.get("people_in_pool") or 0))
        poolside = max(0, int(data.get("people_poolside") or 0))
        submerged = max(0, int(data.get("submerged_count") or 0))

    except (TypeError, ValueError):
        return fail("Occupancy values must be valid integers.", 400)

    try:
        db.log_occupancy(
            cam_id,
            in_pool,
            poolside,
            submerged,
        )

    except Exception:
        return fail("Could not save occupancy data.", 500)

    return jsonify({
        "status": "ok",
        "camera_id": cam_id,
    }), 200


@bp.get("/occupancy")
def get_occupancy():
    """Return the latest occupancy reading for dashboard updates."""

    try:
        rows = db.get_latest_occupancy(limit=1)

        if not rows:
            return jsonify({
                "camera_id": "cam1",
                "people_in_pool": 0,
                "people_poolside": 0,
                "submerged_count": 0,
                "timestamp": db.now_str(),
            })

        # get_latest_occupancy() returns a list, not a dictionary.
        return jsonify(rows[0])

    except Exception:
        return jsonify({
            "camera_id": "cam1",
            "people_in_pool": 0,
            "people_poolside": 0,
            "submerged_count": 0,
            "timestamp": db.now_str(),
            "error": "Occupancy data is temporarily unavailable.",
        }), 503


# ============================================================
# USER ACCOUNT MANAGEMENT
# ============================================================

@bp.get("/users")
@admin_required
def users():
    try:
        return jsonify(db.list_users())
    except Exception:
        return fail("Could not retrieve user accounts.", 500)


@bp.post("/users/approve")
@admin_required
def approve_user():
    user_id = json_body().get("userId")

    if not user_id:
        return fail("Missing userId.", 400)

    try:
        updated = db.approve_user(user_id)

        if updated <= 0:
            return fail("User was not found or could not be approved.", 404)

        return jsonify({"success": True})

    except Exception:
        return fail("Could not approve the account.", 500)


@bp.post("/users/reject")
@admin_required
def reject_user():
    user_id = json_body().get("userId")

    if not user_id:
        return fail("Missing userId.", 400)

    if str(user_id) == str(current_user()["id"]):
        return fail("You can't remove your own account.", 400)

    try:
        ok, info = db.delete_user(user_id)

        if not ok:
            return fail(info, 400)

        return jsonify({
            "success": True,
            "username": info,
        })

    except Exception:
        return fail("Could not delete the account.", 500)


@bp.post("/users/unlock")
@admin_required
def unlock_user():
    username = str(
        json_body().get("username") or ""
    ).strip()

    if not username:
        return fail("Missing username.", 400)

    try:
        db.reset_throttle(username)

        db.log_event(
            username,
            "ACCOUNT_UNLOCKED",
            True,
            client_ip(),
            request.user_agent.string,
            f"Unlocked by {current_user()['username']}",
        )

        return jsonify({"success": True})

    except Exception:
        return fail("Could not unlock the account.", 500)


# ============================================================
# LOGIN ACTIVITY AND SECURITY ALERTS
# ============================================================

@bp.get("/login_logs")
@admin_required
def login_logs():
    limit = request.args.get("limit", default=100, type=int)
    limit = max(1, min(limit or 100, 500))

    try:
        return jsonify(db.get_login_logs(limit))
    except Exception:
        return fail("Could not retrieve login activity.", 500)


@bp.get("/security_alerts")
@admin_required
def security_alerts():
    try:
        return jsonify(db.get_admin_alerts(only_open=True))
    except Exception:
        return fail("Could not retrieve security alerts.", 500)


@bp.post("/security_alerts/ack")
@admin_required
def ack_security_alert():
    alert_id = json_body().get("id")

    if alert_id is None:
        return fail("Missing alert ID.", 400)

    try:
        updated = db.ack_admin_alert(alert_id)

        if not updated:
            return fail("Alert not found or already unavailable.", 404)

        return jsonify({"success": True})

    except (TypeError, ValueError):
        return fail("Invalid alert ID.", 400)

    except Exception:
        return fail("Could not acknowledge the alert.", 500)


# ============================================================
# INCIDENT MANAGEMENT
# ============================================================

@bp.get("/incidents")
@admin_required
def incidents():
    try:
        return jsonify(db.list_incidents())
    except Exception:
        return fail("Could not retrieve incidents.", 500)


def _serve_media(incident_id, kind):
    """Serve local footage or redirect to a temporary cloud URL."""

    try:
        media = footage.open_media(incident_id, kind)

        if not media:
            return fail(f"No {kind} for this incident.", 404)

        source, data, mime = media

        if source == "cloud":
            secure_url = storage.get_secure_video_url(data)

            if secure_url:
                return redirect(secure_url)

            return fail(
                f"Could not generate secure playback URL for {kind}.",
                500,
            )

        extension = "mp4" if kind == "video" else "jpg"

        file_data = (
            data
            if source == "file"
            else BytesIO(data)
        )

        return send_file(
            file_data,
            mimetype=mime,
            conditional=True,
            download_name=f"incident_{incident_id}.{extension}",
            as_attachment=request.args.get("download") == "1",
        )

    except Exception:
        return fail(f"Could not retrieve incident {kind}.", 500)


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
    try:
        success = footage.delete_incident(incident_id)

        if not success:
            return fail("Incident could not be deleted.", 404)

        return jsonify({"success": True})

    except Exception:
        return fail("Could not delete the incident.", 500)


# ============================================================
# FOOTAGE MANAGEMENT
# ============================================================

@bp.get("/footage")
@admin_required
def footage_overview():
    try:
        return jsonify(footage.folder_stats())
    except Exception:
        return fail("Could not retrieve footage information.", 500)


@bp.get("/footage/<day>")
@admin_required
def footage_day(day):
    if not footage.is_valid_day(day):
        return fail("Invalid date.", 400)

    try:
        return jsonify(db.list_incidents_for_day(day))
    except ValueError:
        return fail("Invalid date.", 400)
    except Exception:
        return fail("Could not retrieve footage for this date.", 500)