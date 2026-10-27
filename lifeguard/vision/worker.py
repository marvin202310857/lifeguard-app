"""Vision worker module for handling camera streams."""
import os
import sys
import logging
import cv2

# Silence OpenCV C-level stderr noise on cloud deployments
os.environ["OPENCV_LOG_LEVEL"] = "OFF"
try:
    cv2.setLogLevel(0)
except Exception:
    pass

logger = logging.getLogger(__name__)

# Central registry of active local workers
workers = {}

class VisionWorker:
    def __init__(self, cam_id, src=0):
        self.cam_id = cam_id
        self.src = src
        self.cap = None
        self.is_active = False
        self.metrics = {
            "online": False,
            "status": "OFFLINE",
            "people_in_pool": 0,
            "people_outside_pool": 0,
            "submerged_count": 0,
            "last_updated": ""
        }
        self._init_camera()

    def _init_camera(self):
        try:
            # Check if source is a physical hardware index (0, 1, etc.)
            if isinstance(self.src, int):
                logger.info(f"[{self.cam_id}] Bypassing local hardware device index {self.src} (Cloud container mode).")
                self.cap = None
                self.is_active = False
                return

            # Suppress stderr during VideoCapture initialization for stream URLs
            stderr_fd = sys.stderr.fileno()
            with open(os.devnull, 'w') as devnull:
                old_stderr = os.dup(stderr_fd)
                os.dup2(devnull.fileno(), stderr_fd)
                try:
                    self.cap = cv2.VideoCapture(str(self.src))
                finally:
                    os.dup2(old_stderr, stderr_fd)
                    os.close(old_stderr)

            self.is_active = self.cap is not None and self.cap.isOpened()
        except Exception as e:
            logger.warning(f"[{self.cam_id}] Camera init safely bypassed: {e}")
            self.cap = None
            self.is_active = False

    def wait_frame(self, timeout=1.0):
        if not self.is_active or self.cap is None:
            return None, {}
        
        try:
            ret, frame = self.cap.read()
            if not ret or frame is None:
                return None, {}
                
            success, jpg = cv2.imencode(".jpg", frame)
            if not success:
                return None, {}
                
            return jpg.tobytes(), {}
        except Exception as e:
            logger.debug(f"[{self.cam_id}] Frame read error: {e}")
            return None, {}