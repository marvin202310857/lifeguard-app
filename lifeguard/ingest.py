"""Ingestion endpoints for frame buffers and telemetry from camera nodes."""
import cv2
import logging
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

bp = Blueprint("ingest", __name__)

# In-memory store for uploaded frame buffers from test scripts / external nodes
LATEST_FRAMES = {}

# Safe camera capture check for cloud mode
try:
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        logger.info("Cloud container detected: Skipping hardware webcam initialization.")
        cap = None
except Exception as e:
    logger.warning(f"Bypassing camera hardware init in cloud environment: {e}")
    cap = None


@bp.post("/api/ingest/frame/<cam_id>")
def ingest_frame(cam_id):
    """Receive JPEG frame bytes from local camera scripts or ESP32."""
    frame_data = request.get_data()
    if not frame_data:
        return jsonify({"status": "error", "message": "No frame data received"}), 400

    # Store latest frame buffer in memory
    LATEST_FRAMES[cam_id] = frame_data
    return jsonify({"status": "ok", "cam_id": cam_id}), 200


@bp.post("/api/occupancy")
def ingest_occupancy():
    """Receive occupancy telemetry payload."""
    data = request.get_json(silent=True) or {}
    cam_id = data.get("camera_id", "cam1")
    return jsonify({"status": "ok", "cam_id": cam_id}), 200