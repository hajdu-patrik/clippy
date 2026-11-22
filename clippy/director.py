"""The virtual camera: per shot a layout mode, the active speaker, a smooth camera path and zoom."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .analysis import ClipAnalysis


@dataclass
class FramePlan:
    mode: str
    cx: float = 0.5
    cy: float = 0.5
    zoom: float = 1.0


def crop_w_norm(an: ClipAnalysis, zoom: float) -> float:
    """Width of the 9:16 crop window as a fraction of source width."""
    ch = an.height / zoom
    return min(1.0, (ch * 9 / 16) / an.width)


def max_zoom(an: ClipAnalysis) -> float:
    return max(1.0, min(1.4, an.height / 873.0))


CAM_PRESENCE = 0.3
CAM_GONE_SECONDS = 2.0
REACTION_GAP = 8.0
REACTION_EVERY = 15.0
REACTION_MAX = 3
REACTION_LEAD, REACTION_HOLD = 0.2, 1.2
REACTION_EDGE = 1.5
REACTION_RISE_DB = 6.0


def in_box(f, box: tuple[float, float, float, float]) -> bool:
    x, y, w, h = box
    return x <= f.cx <= x + w and y <= f.cy <= y + h


def _stream_mode(an: ClipAnalysis, s: int, e: int, facecam) -> str:
    """A stream shot keeps the split while the streamer's camera is where it was found; when the stream switches
    scene (the camera full screen, a cutscene, a "be right back" card) the shot follows the face or shows the whole
    frame instead of a crop of the wrong corner."""
    if facecam is None:
        return "stream"
    with_cam = sum(any(in_box(f, facecam) for f in an.faces[i]) for i in range(s, e))
    if with_cam >= CAM_PRESENCE * (e - s):
        return "stream"
    sizes = [max(f.h for f in an.faces[i]) for i in range(s, e) if an.faces[i]]
    if len(sizes) >= 0.5 * (e - s) and float(np.median(sizes)) >= 0.07:
        return "crop"
    return "fit"


def cam_gone(an: ClipAnalysis, s: int, e: int, facecam) -> list[tuple[int, int]]:
    """Stretches inside a stream shot (at least CAM_GONE_SECONDS) with no face in the camera's spot: the stream
    changed scene without a cut the analysis saw, so those frames show the whole picture instead of an empty corner."""
    if facecam is None:
        return []
    win = max(1, int(CAM_GONE_SECONDS * an.fps))
    seen = np.array([any(in_box(f, facecam) for f in an.faces[i]) for i in range(s, e)], dtype=float)
    share = np.convolve(seen, np.ones(win) / win, mode="same")
    gone = share < CAM_PRESENCE
    out, start = [], None
    for k, g in enumerate(list(gone) + [False]):
        if g and start is None:
            start = k
        elif not g and start is not None:
            if k - start >= win:
                out.append((s + start, s + k))
            start = None
    return out


def reaction_windows(an: ClipAnalysis, env_db, hop: float, facecam) -> list[tuple[int, int]]:
    """The streamer's biggest reactions: the loudest moments while the face in the camera has its mouth open, at
    least REACTION_GAP apart, one per REACTION_EVERY seconds (at most REACTION_MAX), never in the first or last
    moments. Each becomes a short window of frames that shows the camera full screen."""
    env = np.asarray(env_db if env_db is not None else [], dtype=float)
    if facecam is None or len(env) < 10 or an.n_frames < an.fps * 4:
        return []
    k = max(1, int(0.25 / hop))
    env = np.convolve(env, np.ones(k) / k, mode="same")
    times = np.arange(an.n_frames) / an.fps
    loud = env[np.minimum((times / hop).astype(int), len(env) - 1)]
    mouth = np.array([max((f.mouth for f in an.faces[i] if in_box(f, facecam)), default=0.0)
                      for i in range(an.n_frames)])
    if not mouth.any():
        return []
    open_mouth = mouth >= np.percentile(mouth[mouth > 0], 60)
    floor = float(np.median(loud)) + REACTION_RISE_DB
    edge = int(REACTION_EDGE * an.fps)
    want = min(REACTION_MAX, max(1, int(an.n_frames / an.fps / REACTION_EVERY)))
    picked: list[int] = []
    for i in np.argsort(-loud):
        i = int(i)
        if loud[i] < floor or len(picked) >= want:
            break
        if i < edge or i > an.n_frames - edge or not open_mouth[i]:
            continue
        if all(abs(i - p) >= REACTION_GAP * an.fps for p in picked):
            picked.append(i)
    return [(max(0, p - int(REACTION_LEAD * an.fps)), min(an.n_frames, p + int(REACTION_HOLD * an.fps)))
            for p in sorted(picked)]


def _shot_modes(an: ClipAnalysis, layout: str, facecam=None) -> list[tuple[int, int, str]]:
    bounds = an.cuts + [an.n_frames]
    shots = []
    for s, e in zip(bounds[:-1], bounds[1:]):
        if e <= s:
            continue
        if layout in ("blur", "fit"):
            mode = "fit"
        elif layout == "stream":
            mode = _stream_mode(an, s, e, facecam)
        elif layout == "center":
            mode = "center"
        elif layout == "face":
            mode = "crop"
        else:
            with_face = [i for i in range(s, e) if an.faces[i]]
            mode = "square" if an.width > an.height else "fit"
            if len(with_face) >= 0.5 * (e - s):
                sizes = [max(f.h for f in an.faces[i]) for i in with_face]
                if float(np.median(sizes)) >= 0.07:
                    mode = "crop"
        shots.append([s, e, mode])
    for k, sh in enumerate(shots):
        if sh[1] - sh[0] < 0.6 * an.fps and len(shots) > 1:
            sh[2] = shots[k - 1][2] if k > 0 else shots[k + 1][2]
    return [tuple(s) for s in shots]


def _smooth_series(vals: list[float], k: int) -> np.ndarray:
    arr = np.asarray(vals, dtype=float)
    if len(arr) < 3 or k < 2:
        return arr
    pad = k // 2
    padded = np.pad(arr, pad, mode="edge")
    med = np.array([np.median(padded[i: i + k]) for i in range(len(arr))])
    kernel = np.ones(k) / k
    return np.convolve(np.pad(med, pad, mode="edge"), kernel, mode="valid")[: len(arr)]


def _targets(an: ClipAnalysis, s: int, e: int, cw: float):
    fps = an.fps
    tracks: dict[int, dict[int, object]] = {}
    for i in range(s, e):
        for f in an.faces[i]:
            tracks.setdefault(f.track, {})[i] = f
    n = e - s
    if not tracks:
        return None, None, set()
    win = max(1, int(0.6 * fps))
    kernel = np.ones(win) / win
    act = {}
    for tid, d in tracks.items():
        arr = np.zeros(n)
        for i, f in d.items():
            arr[i - s] = f.mouth
        act[tid] = np.convolve(arr, kernel, mode="same")

    current, hold_until, challenge = None, s, 0
    last_seen: dict[int, int] = {}
    switches: set[int] = set()
    tx: list[float | None] = []
    ty: list[float | None] = []
    last_pos = None
    for i in range(s, e):
        present = [t for t in tracks if i in tracks[t]]
        for t in present:
            last_seen[t] = i
        if present:
            if current is None or i - last_seen.get(current, -10 ** 9) > 0.4 * fps:
                new = max(present, key=lambda t: tracks[t][i].w * tracks[t][i].h)
                if current is not None and new != current:
                    switches.add(i - s)
                current, hold_until, challenge = new, i + int(1.0 * fps), 0
            else:
                best = max(present, key=lambda t: act[t][i - s])
                if best != current and i >= hold_until and act[best][i - s] > 1.35 * act[current][i - s] + 0.003:
                    challenge += 1
                else:
                    challenge = 0
                if challenge >= int(0.5 * fps):
                    current, hold_until, challenge = best, i + int(1.5 * fps), 0
                    switches.add(i - s)
            faces_now = [tracks[t][i] for t in present]
            big = [f for f in faces_now if f.h >= 0.5 * max(g.h for g in faces_now)]
            if len(big) >= 2:
                left, right = min(f.x for f in big), max(f.x + f.w for f in big)
                if right - left < 0.8 * cw:
                    last_pos = ((left + right) / 2, float(np.mean([f.eye_y for f in big])))
                    tx.append(last_pos[0])
                    ty.append(last_pos[1])
                    continue
            f = tracks[current].get(i)
            if f is not None:
                last_pos = (f.cx, f.eye_y)
        tx.append(last_pos[0] if last_pos else None)
        ty.append(last_pos[1] if last_pos else None)

    first = next((k for k, v in enumerate(tx) if v is not None), None)
    if first is None:
        return None, None, switches
    for k in range(first):
        tx[k], ty[k] = tx[first], ty[first]
    sm = max(3, int(0.2 * fps) | 1)
    return _smooth_series(tx, sm), _smooth_series(ty, sm), switches


def _camera(tx: np.ndarray, ty: np.ndarray, switches: set[int], fps: float, dz: float, cut_dist: float):
    """Locked-off camera with dead zone + critically damped spring follow; hard cut on speaker switch."""
    dt = 1.0 / fps
    omega = 2 * math.pi * 0.75
    cx = gx = float(tx[0])
    cy = gy = float(ty[0])
    vx = vy = 0.0
    out = []
    for k in range(len(tx)):
        x, y = float(tx[k]), float(ty[k])
        if k in switches and abs(x - cx) > cut_dist:
            cx = gx = x
            cy = gy = y
            vx = vy = 0.0
        else:
            if abs(x - gx) > dz:
                gx = x
            if abs(y - gy) > dz * 0.8:
                gy = y
            ax = -omega * omega * (cx - gx) - 2 * omega * vx
            vx += ax * dt
            cx += vx * dt
            ay = -omega * omega * (cy - gy) - 2 * omega * vy
            vy += ay * dt
            cy += vy * dt
        out.append((cx, cy))
    return out


def plan_frames(an: ClipAnalysis, layout: str = "auto", join_frames=(), sentence_frames=(),
                punch: bool = True, facecam=None, reactions=()) -> tuple[list[FramePlan], list[tuple[int, int, str]]]:
    """`facecam` and `reactions` (frame windows) only matter for the stream layout: its shots follow scene switches,
    and its reactions show the streamer's camera full screen."""
    fps = an.fps
    shots = _shot_modes(an, layout, facecam)
    plans: list[FramePlan] = [FramePlan("fit")] * an.n_frames
    zmax = max_zoom(an)
    joins, sents = set(join_frames), set(sentence_frames)
    for s, e, mode in shots:
        if mode in ("fit", "square", "stream"):
            for i in range(s, e):
                plans[i] = FramePlan(mode)
            if mode == "stream":
                for a, b in reactions:
                    for i in range(max(a, s), min(b, e)):
                        plans[i] = FramePlan("cam")
                for a, b in cam_gone(an, s, e, facecam):
                    for i in range(a, b):
                        plans[i] = FramePlan("fit")
            continue

        face_h = [max(f.h for f in an.faces[i]) for i in range(s, e) if an.faces[i]]
        base = 1.0
        if mode == "crop" and face_h and float(np.median(face_h)) < 0.10:
            base = min(zmax, 1.2)
        punch_z = min(zmax, base * 1.10)
        can_punch = punch and punch_z >= base * 1.04
        cw = crop_w_norm(an, base)

        if mode == "center":
            tx = ty = None
        else:
            tx, ty, switches = _targets(an, s, e, cw)
        if tx is None:
            cams = [(0.5, None)] * (e - s)
        else:
            cams = _camera(tx, ty, switches, fps, dz=0.12 * cw, cut_dist=0.35 * cw)

        z, last_change = base, s
        for i in range(s, e):
            if can_punch and i > s:
                if i in joins and i - last_change >= int(0.5 * fps):
                    z, last_change = (punch_z if z == base else base), i
                elif i in sents and i - last_change >= int(4.0 * fps) and i - s >= int(1.0 * fps):
                    z, last_change = (punch_z if z == base else base), i
            cx, eye = cams[i - s]
            cwz = crop_w_norm(an, z)
            ch = 1.0 / z
            cx = min(max(cx, cwz / 2), 1 - cwz / 2)
            cy = 0.5 if (eye is None or ch >= 0.999) else min(max(eye + 0.12 * ch, ch / 2), 1 - ch / 2)
            plans[i] = FramePlan("crop", cx, cy, z)
    return plans, shots


FACECAM_DRIFT = 0.05
FACECAM_PRESENCE = 0.4
FACECAM_MAX_WIDTH = 0.2


def guess_facecam(an: ClipAnalysis) -> tuple[float, float, float, float] | None:
    """For stream layouts: a 16:9 box (normalised) around the usually small, static streamer face: the spot where a face
    is found in the most frames, so the faces of game characters around it do not pull it away. A spot without a face
    in enough frames (only moving faces, a cutscene) is no facecam: then there is none, and the frame is shown whole."""
    flat = [f for fl in an.faces for f in fl]
    if len(flat) < max(3, an.n_frames * 0.3):
        return None

    def near(f, c) -> bool:
        return abs(f.cx - c.cx) <= FACECAM_DRIFT and abs(f.cy - c.cy) <= FACECAM_DRIFT

    step = max(1, len(flat) // 200)
    anchor = max(flat[::step], key=lambda c: sum(any(near(f, c) for f in fl) for fl in an.faces))
    held = [f for f in flat if near(f, anchor)]
    cx = float(np.median([f.cx for f in held]))
    cy = float(np.median([f.cy for f in held]))
    fw = float(np.median([f.w for f in held]))
    steady = sum(any(near(f, anchor) for f in fl) for fl in an.faces)
    if steady < FACECAM_PRESENCE * an.n_frames or fw > FACECAM_MAX_WIDTH:
        return None
    bw = min(1.0, fw * 3.4)
    bh = min(1.0, bw * an.width / an.height * 9 / 16)
    x = max(0.0, min(1.0 - bw, cx - bw / 2))
    y = max(0.0, min(1.0 - bh, cy - bh * 0.45))
    return x, y, bw, bh
