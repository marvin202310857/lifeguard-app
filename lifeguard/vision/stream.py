"""MJPEG stream reader shared by the monitoring server and the calibration tool."""
import logging
import threading
import time

import cv2
import numpy as np
import requests

log = logging.getLogger("lifeguard.stream")

_SOI, _EOI = b"\xff\xd8", b"\xff\xd9"       # JPEG start / end markers
_MAX_BUFFER = 1_000_000


class ESP32StreamReader:
    """Reads an ESP32-CAM MJPEG stream in a background thread and reconnects forever."""

    def __init__(self, url):
        self.url = url
        self._frame = None
        self._seq = 0
        self._last_time = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"stream-{url}")
        self._thread.start()

    # -- background thread
    def _run(self):
        while not self._stop.is_set():
            try:
                with requests.get(self.url, stream=True, timeout=5) as resp:
                    resp.raise_for_status()
                    buf = bytearray()
                    for chunk in resp.iter_content(chunk_size=4096):
                        if self._stop.is_set():
                            return
                        if chunk:
                            buf += chunk
                            self._drain(buf)
            except Exception as exc:  # network errors, bad frames, ...
                log.warning("[%s] stream error: %s - reconnecting", self.url, exc)
            self._stop.wait(2)

    def _drain(self, buf):
        """Pull every complete JPEG out of buf and publish the newest decodable one."""
        while True:
            start = buf.find(_SOI)
            if start == -1:
                if len(buf) > _MAX_BUFFER:
                    buf.clear()
                return
            end = buf.find(_EOI, start + 2)      # search AFTER the start marker
            if end == -1:
                del buf[:start]                  # drop leading garbage, wait for more data
                if len(buf) > _MAX_BUFFER:
                    buf.clear()
                return
            jpg = bytes(buf[start:end + 2])
            del buf[:end + 2]
            frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
            if frame is not None:
                with self._lock:
                    self._frame, self._last_time = frame, time.time()
                    self._seq += 1

    # -- public API
    def read(self, after_seq=-1):
        """-> (frame_copy or None, sequence_number, age_in_seconds)

        The frame is None when nothing has arrived yet, or when the newest frame
        has the sequence number `after_seq` (so callers skip the copy and the work).
        """
        with self._lock:
            if self._frame is None:
                return None, 0, float("inf")
            age = time.time() - self._last_time
            if self._seq == after_seq:
                return None, self._seq, age
            return self._frame.copy(), self._seq, age

    def release(self):
        self._stop.set()
