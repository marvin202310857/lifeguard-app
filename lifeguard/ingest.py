"""Ingestion endpoints for frame buffers and telemetry from camera nodes."""
import time
import logging
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

bp = Blueprint("ingest", __name__)

# Thread-safe in-memory store for frame buffers uploaded by remote camera nodes
LATEST_FRAMES = {}

@bp.post("/api/ingest/frame/<cam_id>")
def ingest_frame(cam_id):
    """Receive JPEG frame bytes from external camera streams (ESP32 / Python scripts)."""
    frame_data = request.get_data()
    if not frame_data:
        return jsonify({"status": "error", "message": "No frame data received"}), 400

    LATEST_FRAMES[cam_id] = frame_data
    return jsonify({"status": "ok", "cam_id": cam_id, "timestamp": time.time()}), 200


@bp.post("/api/occupancy")
def ingest_occupancy():
    """Receive occupancy and telemetry payload from edge workers."""
    data = request.get_json(silent=True) or {}
    cam_id = data.get("camera_id", "cam1")
    return jsonify({"status": "ok", "cam_id": cam_id}), 200