"""Vision worker module for handling camera streams."""
import cv2
import logging

logger = logging.getLogger(__name__)

class VisionWorker:
    def __init__(self, cam_id, src=0):
        self.cam_id = cam_id
        self.src = src
        self.cap = None
        self.is_active = False
        self._init_camera()

    def _init_camera(self):
        try:
            # Check if source is a physical device index (e.g. 0, 1)
            if isinstance(self.src, int):
                logger.info(f"[{self.cam_id}] Attempting local camera index {self.src}...")
                self.cap = cv2.VideoCapture(self.src)
                if not self.cap.isOpened():
                    logger.warning(f"[{self.cam_id}] Local camera {self.src} unavailable (cloud mode active).")
                    self.cap = None
                    return
            else:
                # Video file or RTSP/HTTP stream URL
                self.cap = cv2.VideoCapture(str(self.src))

            self.is_active = self.cap is not None and self.cap.isOpened()
        except Exception as e:
            logger.warning(f"[{self.cam_id}] Camera init skipped: {e}")
            self.cap = None
            self.is_active = False

    def wait_frame(self, timeout=1.0):
        if not self.is_active or self.cap is None:
            return None, {}
        
        ret, frame = self.cap.read()
        if not ret:
            return None, {}
            
        success, jpg = cv2.imencode(".jpg", frame)
        if not success:
            return None, {}
            
        return jpg.tobytes(), {}