"""Per-frame analysis of a clip span: shot cuts, YuNet faces, face tracks and mouth activity."""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .tools import ROOT, ffmpeg_path, fps_rational, log, probe_video, report

YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
             "face_detection_yunet_2023mar.onnx")
YUNET_PATH = ROOT / "models" / "face_detection_yunet_2023mar.onnx"
YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
ANALYSIS_WIDTH = 640
DETECT_WORKERS = min(8, os.cpu_count() or 4)
DETECT_AHEAD = 32
_local = threading.local()
_yunet_ok: bool | None = None


@dataclass
class FaceObs:
    x: float
    y: float
    w: float
    h: float
    score: float
    eye_y: float
    track: int = -1
    mouth: float = 0.0

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


@dataclass
class ClipAnalysis:
    fps: float
    n_frames: int
    width: int
    height: int
    faces: list[list[FaceObs]]
    cuts: list[int]
    detector: str


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ensure_yunet() -> bool:
    """The model is only loaded when its SHA-256 matches the pinned one (supply-chain integrity)."""
    global _yunet_ok
    if _yunet_ok is not None:
        return _yunet_ok
    try:
        if not YUNET_PATH.exists() or _sha256(YUNET_PATH.read_bytes()) != YUNET_SHA256:
            import requests

            log("[faces] downloading the YuNet face detector (~230 KB, once)…")
            r = requests.get(YUNET_URL, timeout=30)
            r.raise_for_status()
            if _sha256(r.content) != YUNET_SHA256:
                raise ValueError("checksum mismatch")
            YUNET_PATH.parent.mkdir(parents=True, exist_ok=True)
            YUNET_PATH.write_bytes(r.content)
        _yunet_ok = True
    except Exception as exc:
        log(f"[faces] YuNet unavailable ({exc}); falling back to the Haar detector.")
        _yunet_ok = False
    return _yunet_ok


class FaceDetector:
    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.kind = "haar"
        if _ensure_yunet():
            try:
                self.det = cv2.FaceDetectorYN.create(str(YUNET_PATH), "", (w, h), 0.6, 0.3, 50)
                self.kind = "yunet"
            except Exception as exc:
                log(f"[faces] YuNet failed to start ({exc}); using the Haar detector.")
        if self.kind == "haar":
            self.det = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

    def detect(self, bgr: np.ndarray, gray: np.ndarray):
        """-> list of (x, y, w, h, score, eye_y, mouth_box, ref_box) in analysis pixels."""
        out = []
        if self.kind == "yunet":
            _, faces = self.det.detect(bgr)
            if faces is None:
                return out
            for f in faces:
                x, y, w, h = (float(v) for v in f[:4])
                _, rey, _, ley, nx, ny, rmx, rmy, lmx, lmy = (float(v) for v in f[4:14])
                score = float(f[14])
                if w < 12 or h < 12:
                    continue
                mw = max(8.0, float(np.hypot(lmx - rmx, lmy - rmy)))
                mcx, mcy = (rmx + lmx) / 2, (rmy + lmy) / 2
                mouth = (mcx - 0.9 * mw, mcy - 0.6 * mw, 1.8 * mw, 1.2 * mw)
                eye_y = (rey + ley) / 2
                ref = (nx - 0.9 * mw, (eye_y + ny) / 2 - 0.5 * mw, 1.8 * mw, 1.0 * mw)
                out.append((x, y, w, h, score, eye_y, mouth, ref))
        else:
            rects = self.det.detectMultiScale(cv2.equalizeHist(gray), 1.1, 6,
                                              minSize=(max(24, self.w // 25), max(24, self.w // 25)))
            for (x, y, w, h) in rects:
                out.append((float(x), float(y), float(w), float(h), 0.8, y + 0.4 * h,
                            (x + 0.25 * w, y + 0.66 * h, 0.5 * w, 0.26 * h),
                            (x + 0.25 * w, y + 0.30 * h, 0.5 * w, 0.20 * h)))
        return out


def _patch(gray: np.ndarray, box) -> np.ndarray | None:
    x, y, w, h = box
    H, W = gray.shape
    x0, y0 = int(max(0, x)), int(max(0, y))
    x1, y1 = int(min(W, x + w)), int(min(H, y + h))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    return cv2.resize(gray[y0:y1, x0:x1], (24, 16), interpolation=cv2.INTER_AREA).astype(np.float32)


def _iou(a, b) -> float:
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ix = max(0.0, min(ax0 + aw, bx0 + bw) - max(ax0, bx0))
    iy = max(0.0, min(ay0 + ah, by0 + bh) - max(ay0, by0))
    inter = ix * iy
    return inter / max(1e-6, aw * ah + bw * bh - inter)


def decode_cmd(video: Path, start: float, duration: float, w: int, h: int, rate: str, matrix: str = "bt709",
               rng: str = "tv", columns: tuple[int, int] | None = None, graph: str | None = None) -> list[str]:
    """Raw BGR frames at a fixed CFR rate (same grid for analysis and render). `columns` (x0, x1): only that
    vertical strip of the converted frame is sent, with exactly the pixels the full frame has there. `graph`: a
    filter chain that builds the w x h frame itself (a fixed layout), converted to BGR at the end."""
    strip = f",crop={columns[1] - columns[0]}:{h}:{columns[0]}:0" if columns else ""
    if graph:
        return [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
                "-i", str(video), "-an", "-sn", "-dn",
                "-vf", f"{graph},scale=in_color_matrix={matrix}:in_range={rng},format=bgr24",
                "-fps_mode", "cfr", "-r", rate, "-f", "rawvideo", "-"]
    return [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
            "-i", str(video), "-an", "-sn", "-dn",
            "-vf", f"scale={w}:{h}:flags=lanczos:in_color_matrix={matrix}:in_range={rng},format=bgr24{strip}",
            "-fps_mode", "cfr", "-r", rate, "-f", "rawvideo", "-"]


def _detector(w: int, h: int) -> FaceDetector:
    """One detector per thread: a DNN net is not safe to share between threads."""
    det = getattr(_local, "det", None)
    if det is None or (det.w, det.h) != (w, h):
        det = _local.det = FaceDetector(w, h)
    return det


def _look(frame: np.ndarray, w: int, h: int):
    """The per-frame work that needs no history (runs in parallel): grey, shot features and the faces."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    thumb = cv2.resize(gray, (64, 36), interpolation=cv2.INTER_AREA).astype(np.float32)
    hsv = cv2.cvtColor(cv2.resize(frame, (96, 54), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    return gray, thumb, hist, _detector(w, h).detect(frame, gray)


def analyze(video: Path, start: float, duration: float) -> ClipAnalysis:
    meta = probe_video(video)
    W, H = meta["width"], meta["height"]
    rate, fps = fps_rational(meta["fps"])
    aw = min(ANALYSIS_WIDTH, W)
    aw -= aw % 2
    ah = int(round(aw * H / W))
    ah -= ah % 2
    det = FaceDetector(aw, ah)

    proc = subprocess.Popen(decode_cmd(video, start, duration, aw, ah, rate, meta["matrix"], meta["range"]),
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=aw * ah * 3 * 4)
    fb = aw * ah * 3
    faces_all: list[list[FaceObs]] = []
    cuts = [0]
    prev_thumb = prev_hist = None
    mad_hist: list[float] = []
    tracks: dict[int, dict] = {}
    next_id = 0
    i = 0
    min_shot = int(0.35 * fps)
    expected = max(1, int(duration * fps))

    def frames():
        """Detection runs ahead on a pool; tracking below consumes the results strictly in frame order."""
        with ThreadPoolExecutor(max_workers=DETECT_WORKERS, thread_name_prefix="clippy-faces") as pool:
            ahead: deque = deque()
            read = 0
            while True:
                if read % 24 == 0:
                    report(read / expected)
                buf = proc.stdout.read(fb)
                if len(buf) < fb:
                    break
                ahead.append(pool.submit(_look, np.frombuffer(buf, np.uint8).reshape(ah, aw, 3), aw, ah))
                read += 1
                if len(ahead) >= DETECT_AHEAD:
                    yield ahead.popleft().result()
            while ahead:
                yield ahead.popleft().result()

    for gray, thumb, hist, found in frames():
        is_cut = False
        if prev_thumb is not None:
            mad = float(np.mean(np.abs(thumb - prev_thumb))) / 255.0
            corr = float(cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL))
            med = float(np.median(mad_hist[-15:])) if mad_hist else 0.0
            if ((mad > max(0.09, 3.5 * med) and corr < 0.85) or (corr < 0.5 and mad > 0.05)) \
                    and i - cuts[-1] >= min_shot:
                is_cut = True
                cuts.append(i)
            mad_hist.append(mad)
        prev_thumb, prev_hist = thumb, hist
        if is_cut:
            tracks.clear()

        obs: list[FaceObs] = []
        for (x, y, w, h, score, eye_y, mbox, rbox) in found:
            box = (x, y, w, h)
            best, best_iou = None, 0.25
            for tid, t in tracks.items():
                if i - t["last"] > int(0.5 * fps):
                    continue
                iou = _iou(box, t["box"])
                dist = np.hypot(x + w / 2 - t["box"][0] - t["box"][2] / 2, y + h / 2 - t["box"][1] - t["box"][3] / 2)
                if iou > best_iou or (best is None and dist < 0.5 * max(w, t["box"][2])):
                    best, best_iou = tid, max(iou, best_iou)
            if best is None or any(o.track == best for o in obs):
                best = next_id
                next_id += 1
                tracks[best] = {"box": box, "last": i, "mouth": None, "ref": None}
            t = tracks[best]
            mp, rp = _patch(gray, mbox), _patch(gray, rbox)
            act = 0.0
            if mp is not None and t["mouth"] is not None and t["last"] == i - 1:
                m_motion = float(np.mean(np.abs(mp - t["mouth"]))) / 255.0
                r_motion = float(np.mean(np.abs(rp - t["ref"]))) / 255.0 if rp is not None and t["ref"] is not None else 0.0
                act = max(0.0, m_motion - 0.8 * r_motion)
            t.update(box=box, last=i, mouth=mp, ref=rp)
            obs.append(FaceObs(x / aw, y / ah, w / aw, h / ah, score, eye_y / ah, best, act))
        faces_all.append(obs)
        i += 1
    proc.stdout.close()
    proc.wait()
    return ClipAnalysis(fps, i, W, H, faces_all, cuts, det.kind)
