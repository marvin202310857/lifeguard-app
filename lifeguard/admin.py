"""Administrator API: accounts, activity log, security alerts and incident footage."""
from io import BytesIO

from flask import Blueprint, jsonify, request, send_file, redirect

from . import db, footage, storage
from .auth import admin_required, client_ip, current_user, fail, json_body

bp = Blueprint("admin", __name__, url_prefix="/api")


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
        # 'data' contains the relative storage path in the cloud bucket
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
    """Daily folders still inside the retention window, with sizes and days left."""
    return jsonify(footage.folder_stats())


@bp.get("/footage/<day>")
@admin_required
def footage_day(day):
    if not footage.is_valid_day(day):
        return fail("Invalid date.", 400)
    return jsonify(db.list_incidents_for_day(day))