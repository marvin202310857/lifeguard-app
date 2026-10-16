import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import cv2
import numpy as np

from lifeguard.vision.stream import ESP32StreamReader


def jpeg(value):
    ok, buf = cv2.imencode(".jpg", np.full((48, 64, 3), value, np.uint8))
    return buf.tobytes()


class MJPEG(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        # leading garbage that contains a stray end-of-image marker (the old parser choked on this)
        self.wfile.write(b"junk\xff\xd9junk")
        for v in (40, 120, 200):
            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg(v) + b"\r\n")
            self.wfile.flush()
            time.sleep(0.05)
        time.sleep(1)

    def log_message(self, *a):
        pass


def test_reader_survives_garbage_and_returns_newest_frame():
    server = HTTPServer(("127.0.0.1", 0), MJPEG)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    reader = ESP32StreamReader(f"http://127.0.0.1:{server.server_port}/stream")
    try:
        deadline = time.time() + 5
        frame, seq = None, 0
        while time.time() < deadline and seq < 3:
            frame, seq, age = reader.read()
            time.sleep(0.05)
        assert seq >= 3 and frame is not None and frame.shape[:2] == (48, 64)
        assert abs(int(frame.mean()) - 200) < 15
        again, same_seq, _ = reader.read(after_seq=seq)
        assert again is None and same_seq == seq        # nothing new -> no copy made
    finally:
        reader.release()
        server.shutdown()
