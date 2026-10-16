"""Click the pool's edge on a live camera frame to set its detection area.

    python -m lifeguard.calibrate --camera-id cam1 [--stream-url http://192.168.1.62:81/stream]

The stream URL defaults to the one in config.json. The polygon is saved to
config.json as "<camera-id>_roi" and picked up by the server without a restart.
"""
import argparse
import time

import cv2
import numpy as np

from . import settings as cfg
from .vision.stream import ESP32StreamReader

HELP_LINES = ("LEFT: add point   RIGHT: undo   N: new frame",
              "R: reset   S: save & exit   Q/Esc: quit without saving")


def on_mouse(event, x, y, _flags, points):
    if event == cv2.EVENT_LBUTTONDOWN:
        points.append((x, y))
        print(f"  + point {len(points)}: ({x}, {y})")
    elif event == cv2.EVENT_RBUTTONDOWN and points:
        print(f"  - removed point: {points.pop()}")


def draw_overlay(frame, points):
    display = frame.copy()
    if len(points) >= 3:
        poly = np.array(points, np.int32).reshape((-1, 1, 2))
        overlay = frame.copy()
        cv2.fillPoly(overlay, [poly], (0, 165, 255))
        display = cv2.addWeighted(overlay, 0.35, display, 0.65, 0)
        cv2.polylines(display, [poly], True, (0, 140, 255), 3)
    elif len(points) == 2:
        cv2.line(display, points[0], points[1], (0, 140, 255), 3)
    for i, point in enumerate(points, 1):
        cv2.circle(display, point, 6, (0, 0, 255), -1)
        cv2.putText(display, str(i), (point[0] + 8, point[1] - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

    cv2.rectangle(display, (0, 0), (660, 95), (0, 0, 0), -1)
    for row, line in enumerate(HELP_LINES):
        cv2.putText(display, line, (10, 25 + 25 * row), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    cv2.putText(display, f"Points placed: {len(points)}  (need at least 3, trace the pool edge closely)",
                (10, 78), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
    return display


def wait_for_frame(stream, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        frame, _, _ = stream.read()
        if frame is not None:
            return frame
        time.sleep(0.2)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--camera-id", required=True, choices=sorted(cfg.CAMERAS))
    parser.add_argument("--stream-url", help="MJPEG stream URL (default: from config.json)")
    args = parser.parse_args()

    config = cfg.load_config()
    url = args.stream_url or config.get(args.camera_id) or cfg.DEFAULT_STREAMS[args.camera_id]
    print(f"Calibrating {args.camera_id} from {url} ...")
    stream = ESP32StreamReader(url)

    frame = wait_for_frame(stream)
    if frame is None:
        print("No frame received after 20s. Check the stream URL and that the camera is online.")
        stream.release()
        return 1

    roi_key = f"{args.camera_id}_roi"
    if config.get(roi_key):
        print(f"A polygon is already saved for '{args.camera_id}'; saving new points will replace it.")

    window = f"Calibrate pool area - {args.camera_id}"
    points = []
    cv2.namedWindow(window)
    cv2.setMouseCallback(window, on_mouse, points)

    saved = False
    while True:
        cv2.imshow(window, draw_overlay(frame, points))
        key = cv2.waitKey(30) & 0xFF
        if key == ord("n"):
            fresh, _, _ = stream.read()
            if fresh is not None:
                frame = fresh
        elif key == ord("r"):
            points.clear()
        elif key == ord("s"):
            if len(points) < 3:
                print("Need at least 3 points to form a polygon.")
                continue
            cfg.set_config_value(roi_key, [list(p) for p in points])
            print(f"Saved {len(points)} points to {cfg.CONFIG_FILE.name} as '{roi_key}'.")
            saved = True
            break
        elif key in (ord("q"), 27):
            print("Quit without saving.")
            break

    stream.release()
    cv2.destroyAllWindows()
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
