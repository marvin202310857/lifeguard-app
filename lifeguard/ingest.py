import time
import requests
import cv2

RENDER_URL = "https://lifeguard-8t4w.onrender.com"
CAM_ID = "cam1"

# Initialize webcam (0 is default laptop camera)
cap = cv2.VideoCapture(0)

if not cap.isOpened():
    print("Error: Could not open camera.")
    exit()

print(f"Streaming webcam feed to {RENDER_URL} as '{CAM_ID}'... Press Ctrl+C to stop.")

while True:
    ret, frame = cap.read()
    if not ret:
        print("Failed to capture image frame.")
        time.sleep(1)
        continue

    # Add timestamp directly to webcam frame
    timestamp_str = time.strftime("%Y-%m-%d %H:%M:%S")
    cv2.putText(frame, f"CAM1 LIVE - {timestamp_str}", (20, 40),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)

    # Encode frame to JPEG
    success, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
    if not success:
        continue

    # 1. Post JPEG image bytes to Render frame ingest endpoint
    try:
        frame_res = requests.post(
            f"{RENDER_URL}/api/ingest/frame/{CAM_ID}",
            data=buffer.tobytes(),
            headers={"Content-Type": "image/jpeg"},
            timeout=3
        )
        print(f"[{CAM_ID}] Frame upload: {frame_res.status_code}")
    except Exception as e:
        print(f"Frame upload error: {e}")

    # 2. Post telemetry data
    try:
        telemetry_res = requests.post(
            f"{RENDER_URL}/api/occupancy",
            json={
                "camera_id": CAM_ID,
                "people_in_pool": 1,
                "people_poolside": 0,
                "submerged_count": 0,
                "status": "SAFE"
            },
            timeout=3
        )
        print(f"[{CAM_ID}] Telemetry upload: {telemetry_res.status_code}")
    except Exception as e:
        print(f"Telemetry upload error: {e}")

    time.sleep(0.5)

cap.release()