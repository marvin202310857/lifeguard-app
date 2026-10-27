import os
import sys
import time
import threading
from datetime import datetime
import cv2
import requests
from ultralytics import YOLO

RENDER_URL = "https://lifeguard-8t4w.onrender.com"
CAM_ID = "cam1"

# Drowning timer thresholds (in seconds)
WARNING_THRESHOLD_SEC = 5   # ORANGE at 5s motionless
DANGER_THRESHOLD_SEC = 10   # RED + RECORDING at 10s motionless

VIDEO_PATH = "sample_pool_video.mp4"
RECORDINGS_DIR = "incidents"
os.makedirs(RECORDINGS_DIR, exist_ok=True)

if not os.path.exists(VIDEO_PATH):
    print(f"Error: '{VIDEO_PATH}' not found in project directory.")
    sys.exit(1)

# Load lightweight YOLOv8 Nano model (auto-downloads on first run)
model = YOLO("yolov8n.pt")

cap = cv2.VideoCapture(VIDEO_PATH)

# Tracker state dictionary: { swimmer_id: {"first_seen": timestamp, "last_pos": (cx, cy), "stationary_since": timestamp} }
tracked_swimmers = {}

video_writer = None
recording_filename = None

def async_send_telemetry(frame_bytes, payload):
    try:
        requests.post(
            f"{RENDER_URL}/api/ingest/frame/{CAM_ID}",
            data=frame_bytes,
            headers={"Content-Type": "image/jpeg"},
            timeout=1.0
        )
        requests.post(
            f"{RENDER_URL}/api/occupancy",
            json=payload,
            timeout=1.0
        )
    except Exception:
        pass

print(f"=== RUNNING YOLOV8 MULTI-PERSON DROWNING DETECTOR FOR {CAM_ID} ===")

last_send_time = 0
SEND_INTERVAL = 0.15  # Keep network ingestion light to prevent video lag

while True:
    ret, frame = cap.read()
    
    if not ret:
        print("[LOOP] Video finished. Restarting playback...")
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        tracked_swimmers.clear()
        if video_writer is not None:
            video_writer.release()
            video_writer = None
        continue

    # Resize frame to 640x360 for high FPS processing and lag-free streaming
    frame = cv2.resize(frame, (640, 360))
    h, w, _ = frame.shape

    # Define Pool ROI Box [x1, y1, x2, y2]
    roi_x1, roi_y1 = int(w * 0.05), int(h * 0.08)
    roi_x2, roi_y2 = int(w * 0.95), int(h * 0.92)

    # Run YOLOv8 Object Tracking for Person Class (class 0)
    results = model.track(frame, persist=True, classes=[0], verbose=False, conf=0.25)
    
    current_time = time.time()
    people_in_pool = 0
    people_poolside = 0
    submerged_count = 0
    active_ids_this_frame = set()

    if results and len(results) > 0 and results[0].boxes is not None:
        boxes = results[0].boxes

        for box in boxes:
            # Extract box coordinates and tracking ID
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            bw, bh = x2 - x1, y2 - y1
            cx, cy = x1 + bw // 2, y1 + bh // 2

            swimmer_id = int(box.id[0]) if box.id is not None else int(cx + cy)
            active_ids_this_frame.add(swimmer_id)

            in_roi = (roi_x1 <= cx <= roi_x2) and (roi_y1 <= cy <= roi_y2)

            if in_roi:
                people_in_pool += 1

                # Initialize tracking record for new swimmer
                if swimmer_id not in tracked_swimmers:
                    tracked_swimmers[swimmer_id] = {
                        "first_seen": current_time,
                        "last_pos": (cx, cy),
                        "stationary_since": current_time
                    }
                else:
                    # Check if the swimmer moved significantly
                    prev_cx, prev_cy = tracked_swimmers[swimmer_id]["last_pos"]
                    distance_moved = ((cx - prev_cx) ** 2 + (cy - prev_cy) ** 2) ** 0.5
                    
                    if distance_moved > 15:  # Swimmer is actively moving/swimming
                        tracked_swimmers[swimmer_id]["stationary_since"] = current_time

                    tracked_swimmers[swimmer_id]["last_pos"] = (cx, cy)

                # Calculate motionless/submerged duration
                stationary_duration = current_time - tracked_swimmers[swimmer_id]["stationary_since"]

                # Determine Alert Status per swimmer
                if stationary_duration >= DANGER_THRESHOLD_SEC:
                    color = (0, 0, 255)       # RED
                    submerged_count += 1
                    status_label = f"ALERT ID#{swimmer_id} ({int(stationary_duration)}s)"
                    
                    # Target circle on motionless/submerged swimmer
                    cv2.circle(frame, (cx, cy), max(25, max(bw, bh) // 2), (0, 0, 255), 2)
                    cv2.circle(frame, (cx, cy), 4, (0, 0, 255), -1)

                    # Trigger automatic incident recording
                    if video_writer is None:
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        recording_filename = os.path.join(RECORDINGS_DIR, f"drowning_alert_{timestamp}.mp4")
                        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                        video_writer = cv2.VideoWriter(recording_filename, fourcc, 15.0, (w, h))
                        print(f"[REC] Drowning alert triggered! Saving video to: {recording_filename}")

                elif stationary_duration >= WARNING_THRESHOLD_SEC:
                    color = (0, 165, 255)     # ORANGE
                    status_label = f"WARNING ID#{swimmer_id} ({int(stationary_duration)}s)"
                else:
                    color = (0, 255, 0)       # GREEN
                    status_label = f"SWIMMER ID#{swimmer_id}"

                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                cv2.putText(frame, status_label, (x1, max(roi_y1 + 15, y1 - 5)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 2)
            else:
                people_poolside += 1
                cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 200, 0), 1)

    # Clean up stale IDs of swimmers who exited the pool frame
    stale_ids = [sid for sid in tracked_swimmers if sid not in active_ids_this_frame]
    for sid in stale_ids:
        del tracked_swimmers[sid]

    # Stop recording when no active drowning alert exists
    if submerged_count == 0 and video_writer is not None:
        print("[RESET] Drowning alert resolved. Finalizing incident clip...")
        video_writer.release()
        video_writer = None

    if video_writer is not None:
        video_writer.write(frame)

    status_state = "DANGER" if submerged_count > 0 else "SAFE"

    # Draw Pool Boundary Box
    roi_color = (0, 0, 255) if submerged_count > 0 else (255, 120, 0)
    cv2.rectangle(frame, (roi_x1, roi_y1), (roi_x2, roi_y2), roi_color, 2)
    cv2.putText(frame, f"POOL ROI | IN POOL: {people_in_pool} | POOLSIDE: {people_poolside}", 
                (roi_x1 + 10, roi_y1 + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, roi_color, 2)

    cv2.imshow("LifeGuard - YOLOv8 Multi-Person Drowning Test", frame)
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

    # Async background HTTP posting to eliminate rendering lag
    if current_time - last_send_time >= SEND_INTERVAL:
        last_send_time = current_time
        success, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 55])
        if success:
            payload = {
                "camera_id": CAM_ID,
                "people_in_pool": people_in_pool,
                "people_poolside": people_poolside,
                "submerged_count": submerged_count,
                "status": status_state
            }
            threading.Thread(
                target=async_send_telemetry,
                args=(buffer.tobytes(), payload),
                daemon=True
            ).start()

if video_writer is not None:
    video_writer.release()

cap.release()
cv2.destroyAllWindows()