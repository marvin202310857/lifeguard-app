"""Endpoints the pool computer uses to send its data to the cloud server.

Every call must carry the secret key (header "Authorization: Bearer <key>") that is set as the
LIFEGUARD_INGEST_KEY environment variable on the cloud server. Without that variable the
endpoints are switched off.
"""
import hmac
import json
import logging
import os
import re

from flask import Blueprint, jsonify, request

from . import db, footage
from . import settings as cfg
from .auth import client_ip, fail
from .cloud import RemoteCamera
from .registry import workers

log = logging.getLogger("lifeguard.ingest")
bp = Blueprint("ingest", __name__, url_prefix="/api/ingest")

MAX_VIDEO_BYTES = 80 * 1024 * 1024
MAX_FRAME_BYTES = 400 * 1024
MAX_SNAPSHOT_BYTES = 3 * 1024 * 1024
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


@bp.before_request
def authenticate():
    key = os.environ.get("LIFEGUARD_INGEST_KEY", "")
    if len(key) < 16:
        return fail("Uploads from the pool computer are not enabled on this server.", 503)
    header = request.headers.get("Authorization", "")
    token = header[7:] if header.startswith("Bearer ") else ""
    if not hmac.compare_digest(token.encode(), key.encode()):
        log.warning("rejected an upload with a wrong key from %s", client_ip())
        return fail("Invalid key.", 401)
    try:                                           # clips are bigger than the normal 1 MB request limit
        request.max_content_length = MAX_VIDEO_BYTES
    except AttributeError:
        pass


def _remote(cam_id):
    camera = workers.get(cam_id)
    return camera if isinstance(camera, RemoteCamera) else None


@bp.post("/heartbeat")
def heartbeat():
    """Live numbers every couple of seconds. The reply says which cameras someone is watching."""
    data = request.get_json(silent=True)
    reports = data.get("cameras") if isinstance(data, dict) and isinstance(data.get("cameras"), dict) else {}
    for cam_id, camera in workers.items():
        report = reports.get(cam_id)
        if isinstance(camera, RemoteCamera) and isinstance(report, dict):
            alerts = report.get("alerts") if isinstance(report.get("alerts"), list) else []
            camera.update(report, alerts)
    return jsonify({"ok": True, "frames": [c for c, cam in workers.items()
                                           if isinstance(cam, RemoteCamera) and cam.wants_frames()]})


@bp.post("/frame/<cam_id>")
def frame(cam_id):
    camera = _remote(cam_id)
    if camera is None:
        return fail("Unknown camera.", 404)
    data = request.get_data()
    if not data.startswith(b"\xff\xd8") or len(data) > MAX_FRAME_BYTES:
        return fail("Not a valid frame.", 400)
    camera.push_frame(data)
    return "", 204


@bp.post("/incident")
def incident():
    try:
        meta = json.loads(request.form.get("meta", ""))
        cam_id, event = meta["cam_id"], str(meta.get("event") or "Drowning Risk Detected")[:100]
        track_id, elapsed = int(meta["track_id"]), int(meta["elapsed_sec"])
        created_at, ref = str(meta["created_at"]), str(meta["client_ref"])
    except (ValueError, KeyError, TypeError):
        return fail("Invalid incident data.", 400)
    if cam_id not in cfg.CAMERAS or not TIMESTAMP_RE.match(created_at) or not 1 <= len(ref) <= 120:
        return fail("Invalid incident data.", 400)
    snapshot = request.files.get("snapshot")
    jpeg = snapshot.read(MAX_SNAPSHOT_BYTES + 1) if snapshot else None
    if jpeg is not None and (len(jpeg) > MAX_SNAPSHOT_BYTES or not jpeg.startswith(b"\xff\xd8")):
        return fail("Invalid snapshot.", 400)
    incident_id = footage.record_incident(cam_id, track_id, elapsed, jpeg, event,
                                          created_at=created_at, client_ref=ref)
    return jsonify({"id": incident_id})


@bp.post("/incident/<int:incident_id>/video")
def video(incident_id):
    row = db.get_incident(incident_id)
    if not row:
        return fail("Unknown incident.", 404)
    data = request.get_data()
    if not data:
        return fail("Empty video.", 400)
    footage.store_video([incident_id], row["cam_id"], data, "video/mp4")
    return jsonify({"success": True})


@bp.post("/incident/<int:incident_id>/resolve")
def resolve(incident_id):
    if not db.get_incident(incident_id):
        return fail("Unknown incident.", 404)
    body = request.get_json(silent=True)
    when = body.get("resolved_at") if isinstance(body, dict) else None
    db.resolve_incident(incident_id, when if isinstance(when, str) and TIMESTAMP_RE.match(when) else None)
    return jsonify({"success": True})
