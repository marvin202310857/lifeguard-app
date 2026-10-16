"""Small OpenCV helpers: JPEG encoding, status frames and incident clips."""
import logging
import os
import shutil
import subprocess
import tempfile

import cv2
import numpy as np

log = logging.getLogger("lifeguard.video")


def jpeg_bytes(frame, quality=80):
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else b""


def text_frame(lines, w=640, h=480):
    """Dark placeholder frame with centred text (offline / loading screens)."""
    img = np.zeros((h, w, 3), np.uint8)
    y = h // 2 - 15 * (len(lines) - 1)
    for line in lines:
        (tw, _), _ = cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
        cv2.putText(img, line, (max(10, (w - tw) // 2), y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 200, 200), 2)
        y += 30
    return img


def _open_writer(path, fps, size):
    for codec in ("avc1", "H264", "mp4v"):          # H.264 first: plays in every browser
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*codec), fps, size)
        if writer.isOpened():
            return writer, codec
        writer.release()
    return None, None


def frames_to_video(frames):
    """frames: [(timestamp, jpeg_bytes)] -> (mp4_bytes, "video/mp4") or (None, None)."""
    if len(frames) < 2:
        return None, None
    first = cv2.imdecode(np.frombuffer(frames[0][1], np.uint8), cv2.IMREAD_COLOR)
    if first is None:
        return None, None
    h, w = first.shape[:2]
    duration = max(0.5, frames[-1][0] - frames[0][0])
    fps = min(25.0, max(2.0, len(frames) / duration))

    fd, raw_path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    final_path = raw_path
    try:
        writer, codec = _open_writer(raw_path, fps, (w, h))
        if writer is None:
            log.error("no usable MP4 encoder found")
            return None, None
        for _, jpg in frames:
            img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            if img.shape[:2] != (h, w):
                img = cv2.resize(img, (w, h))
            writer.write(img)
        writer.release()

        # mp4v does not play in Chrome; convert to H.264 when ffmpeg is available.
        if codec == "mp4v" and shutil.which("ffmpeg"):
            final_path = raw_path + ".h264.mp4"
            result = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", raw_path, "-c:v", "libx264",
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart", final_path],
                capture_output=True, timeout=120)
            if result.returncode != 0 or not os.path.exists(final_path):
                final_path = raw_path
        if not os.path.exists(final_path) or os.path.getsize(final_path) == 0:
            return None, None
        with open(final_path, "rb") as fh:
            return fh.read(), "video/mp4"
    finally:
        for path in {raw_path, final_path}:
            try:
                os.remove(path)
            except OSError:
                pass
