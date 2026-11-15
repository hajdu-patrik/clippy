"""Turn one source frame into one 1080x1920 frame. One strategy per layout mode; add a layout by adding a class."""

from __future__ import annotations

from typing import Protocol

import cv2
import numpy as np

from .director import FramePlan

OUT_W, OUT_H = 1080, 1920


class Compositor(Protocol):
    def compose(self, frame: np.ndarray, plan: FramePlan) -> np.ndarray: ...


def cover(img: np.ndarray, w: int, h: int) -> np.ndarray:
    """Centre-crop to the w:h aspect, then resize (area filter down, Lanczos up)."""
    H, W = img.shape[:2]
    target = w / h
    if W / H > target:
        nw = int(H * target)
        img = img[:, (W - nw) // 2: (W - nw) // 2 + nw]
    else:
        nh = int(W / target)
        img = img[(H - nh) // 2: (H - nh) // 2 + nh]
    up = img.shape[1] < w
    return cv2.resize(img, (w, h), interpolation=cv2.INTER_LANCZOS4 if up else cv2.INTER_AREA)


class CropCompositor:
    """Sub-pixel Lanczos 9:16 window following the virtual camera."""

    REACH = 8

    @staticmethod
    def window(W: int, H: int, plan: FramePlan) -> tuple[float, float, float, float]:
        """(x0, y0, width, height) of the source region this plan shows, in source pixels."""
        ch = H / plan.zoom
        cw = ch * 9 / 16
        if cw > W:
            cw, ch = W, W * 16 / 9
        return min(max(plan.cx * W - cw / 2, 0.0), W - cw), min(max(plan.cy * H - ch / 2, 0.0), H - ch), cw, ch

    @classmethod
    def columns(cls, W: int, H: int, plans) -> tuple[int, int] | None:
        """The vertical strip every plan reads (plus the Lanczos reach), on even pixels; None when it is most of
        the frame or a plan is not a crop. Decoding only this strip moves far fewer bytes through the pipe."""
        if not plans or any(p.mode != "crop" for p in plans):
            return None
        spans = [cls.window(W, H, p) for p in plans]
        x0 = max(0, int(min(x for x, _, _, _ in spans)) - cls.REACH)
        x1 = min(W, int(max(x + w for x, _, w, _ in spans)) + 1 + cls.REACH)
        x0 -= x0 % 2
        x1 += x1 % 2
        return (x0, min(W, x1)) if min(W, x1) - x0 < 0.8 * W else None

    def compose(self, frame: np.ndarray, plan: FramePlan) -> np.ndarray:
        H, W = frame.shape[:2]
        x0, y0, cw, _ = self.window(W, H, plan)
        s = OUT_W / cw
        m = np.float32([[s, 0, -x0 * s], [0, s, -y0 * s]])
        return cv2.warpAffine(frame, m, (OUT_W, OUT_H), flags=cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE)


class FitCompositor:
    """The whole frame on a darkened, blurred copy of itself."""

    def compose(self, frame: np.ndarray, plan: FramePlan) -> np.ndarray:
        H, W = frame.shape[:2]
        bg = cv2.GaussianBlur(cover(frame, 108, 192), (0, 0), 5)
        bg = cv2.convertScaleAbs(cv2.resize(bg, (OUT_W, OUT_H), interpolation=cv2.INTER_CUBIC), alpha=0.55, beta=0)
        fh = int(round(OUT_W * H / W))
        if fh > OUT_H:
            fw = int(round(OUT_H * W / H))
            fg = cv2.resize(frame, (fw, OUT_H), interpolation=cv2.INTER_AREA if H > OUT_H else cv2.INTER_LANCZOS4)
            bg[:, (OUT_W - fw) // 2: (OUT_W - fw) // 2 + fw] = fg
            return bg
        fg = cv2.resize(frame, (OUT_W, fh), interpolation=cv2.INTER_AREA if W > OUT_W else cv2.INTER_LANCZOS4)
        bg[(OUT_H - fh) // 2: (OUT_H - fh) // 2 + fh] = fg
        return bg


class StreamCompositor:
    """Facecam on top, gameplay below, a thin separator between them."""

    TOP_H = 730

    def __init__(self, facecam: tuple[float, float, float, float]):
        self.facecam = facecam

    def compose(self, frame: np.ndarray, plan: FramePlan) -> np.ndarray:
        H, W = frame.shape[:2]
        x, y, w, h = self.facecam
        cam = frame[int(y * H): int(y * H) + max(2, int(h * H)), int(x * W): int(x * W) + max(2, int(w * W))]
        out = np.vstack([cover(cam, OUT_W, self.TOP_H), cover(frame, OUT_W, OUT_H - self.TOP_H)])
        out[self.TOP_H - 3: self.TOP_H + 3] = 0
        return out


class LayoutCompositor:
    """Dispatches each frame to the compositor of its plan's mode."""

    def __init__(self, facecam: tuple[float, float, float, float] | None = None):
        self.by_mode: dict[str, Compositor] = {"crop": CropCompositor(), "fit": FitCompositor()}
        if facecam:
            self.by_mode["stream"] = StreamCompositor(facecam)

    def compose(self, frame: np.ndarray, plan: FramePlan) -> np.ndarray:
        return self.by_mode.get(plan.mode, self.by_mode["fit"]).compose(frame, plan)
