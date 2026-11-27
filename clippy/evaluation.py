"""`clippy eval`: how good the clip picker's choices are on a golden set of labelled moments, and what each picking
step costs, so a faster picker can be checked against the one before it. The format is in docs/quality.md."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from . import highlights, i18n
from .boundaries import COMPLETE_PAUSE, NONE, Boundaries
from .highlights import Clip, find_highlights
from .llm import think_enabled
from .signals import Signals
from .storage import Storage
from .tools import ClippyError, log, read_json, write_json

FORMAT = 1
DEFAULT_GRADE = 2
GRADES = (1, 2, 3)
RECALL_AT = (1, 3, 5)
OVERLAP = 0.5
WORD_SLACK = 0.1
DEFAULTS = {"clips": 5, "min_len": 20.0, "max_len": 90.0}
STEPS = ("windows", "rerank", "starts", "ends", "texts", "polish", "proofread", "other")
STEP_SCHEMAS = {"CLIPS_SCHEMA": "windows", "RERANK_SCHEMA": "rerank", "TEXTS_SCHEMA": "texts",
                "POLISH_SCHEMA": "polish", "PROOFREAD_SCHEMA": "proofread"}
SIGNAL_KINDS = ("vc", "v", "c", "")
RATES = ("recall@1", "recall@3", "recall@5", "recall", "recall_graded", "precision", "start_ok", "end_ok", "mid_cut")


def step_of(system: str, schema) -> str:
    """Which picking step an LLM call belongs to, told by the answer shape it asks for."""
    for name, step in STEP_SCHEMAS.items():
        if schema is not None and schema is getattr(highlights, name, None):
            return step
    if schema is not None and schema is getattr(highlights, "EDGES_SCHEMA", None):
        starts = getattr(highlights, "STARTS_PROMPT", "").split("\n", 1)[0]
        return "starts" if starts and system.split("\n", 1)[0] == starts else "ends"
    return "other"


class MeteredLLM:
    """An LLM that counts its calls and their seconds per picking step; everything else goes to the real one."""

    def __init__(self, llm):
        self._llm = llm
        self.steps: dict[str, dict] = {}

    def chat_json(self, system: str, user: str, *args, **kwargs):
        schema = kwargs.get("schema", args[1] if len(args) > 1 else None)
        step = self.steps.setdefault(step_of(system, schema), {"calls": 0, "seconds": 0.0, "failed": 0})
        t0 = time.perf_counter()
        try:
            return self._llm.chat_json(system, user, *args, **kwargs)
        except Exception:
            step["failed"] += 1
            raise
        finally:
            step["calls"] += 1
            step["seconds"] = round(step["seconds"] + time.perf_counter() - t0, 3)

    def __getattr__(self, name: str):
        return getattr(self._llm, name)


def overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    """The shared time of two spans as a share of the shorter one."""
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    return inter / max(1e-6, min(a[1] - a[0], b[1] - b[0]))


def edges(clip: Clip, bounds: Boundaries) -> dict | None:
    """Whether a clip starts where a sentence starts, ends where a thought is complete, and whether either edge cuts
    into a sentence or a word (words whose aligned times overlap are not a cut); None for a clip without words."""
    words = bounds.words
    i0, i1 = bounds.index_at_start(clip.start - 0.01), bounds.index_at_end(clip.end)
    if i0 is None or i1 is None or i1 < i0:
        return None
    before, after = (words[i0 - 1] if i0 > 0 else None), (words[i1 + 1] if i1 + 1 < len(words) else None)
    cut_word = ((before is not None and clip.start + WORD_SLACK < before["end"] <= words[i0]["start"] + WORD_SLACK)
                or (after is not None and words[i1]["end"] - WORD_SLACK <= after["start"] < clip.end - WORD_SLACK))
    start_ok, level = bounds.opens[i0], bounds.level[i1]
    return {"start_ok": start_ok, "end_ok": level >= COMPLETE_PAUSE, "mid_cut": cut_word or not start_ok or level == NONE}


def grade_of(moment: dict) -> int:
    grade = moment.get("grade")
    return grade if grade in GRADES else DEFAULT_GRADE


def score(picks: list[Clip], moments: list[dict], bounds: Boundaries | None, threshold: float = OVERLAP) -> dict:
    """The counts behind every rate of one video: labelled moments found among the first k picks, picks that hit a
    label, and how the picks' edges sit in the speech."""
    spans = [(float(m["start"]), float(m["end"])) for m in moments]
    hit = [[overlap((c.start, c.end), s) >= threshold for s in spans] for c in picks]
    counts = {"picks": len(picks), "labels": len(spans), "graded": sum(grade_of(m) for m in moments),
              "matched": sum(any(row) for row in hit), "spoken": 0, "start_ok": 0, "end_ok": 0, "mid_cut": 0}
    for k in (*RECALL_AT, None):
        found = [j for j in range(len(spans)) if any(row[j] for row in hit[:k])]
        counts["found" if k is None else f"found@{k}"] = len(found)
        if k is None:
            counts["found_graded"] = sum(grade_of(moments[j]) for j in found)
    for c in picks:
        e = edges(c, bounds) if bounds is not None and bounds.words else None
        if e is not None:
            counts["spoken"] += 1
            for key in ("start_ok", "end_ok", "mid_cut"):
                counts[key] += bool(e[key])
    return counts


def rates(counts: dict) -> dict:
    """Shares from counts; None where nothing was there to count (no labels, no picks with words)."""
    def share(num: str, den: str) -> float | None:
        return round(counts[num] / counts[den], 4) if counts.get(den) else None

    out = {f"recall@{k}": share(f"found@{k}", "labels") for k in RECALL_AT}
    out |= {"recall": share("found", "labels"), "recall_graded": share("found_graded", "graded"),
            "precision": share("matched", "picks") if counts.get("labels") else None,
            "start_ok": share("start_ok", "spoken"), "end_ok": share("end_ok", "spoken"),
            "mid_cut": share("mid_cut", "spoken")}
    return out


def _resolve(folder: Path, name: str | None) -> Path | None:
    if not name:
        return None
    path = Path(name)
    return path if path.is_absolute() else folder / path


def load_golden(path: Path) -> dict:
    if not path.is_file():
        raise ClippyError(key="error.eval_no_golden", path=path)
    try:
        golden = read_json(path)
        videos = golden["videos"]
        for v in videos:
            if not str(v.get("id") or "") or not v.get("transcript"):
                raise ValueError("every video needs an id and a transcript")
            for m in v.get("moments") or []:
                if not float(m["end"]) > float(m["start"]):
                    raise ValueError(f"{v['id']}: a moment ends before it starts ({m['start']}-{m['end']})")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ClippyError(key="error.eval_golden", path=path, detail=str(exc)[:200]) from exc
    return golden


def _turns(path: Path | None):
    try:
        turns = read_json(path) if path else None
    except (OSError, ValueError):
        turns = None
    if not isinstance(turns, list):
        return None
    return lambda sents: turns if len(turns) == len(sents) else []


def run_video(video: dict, storage: Storage, llm, threshold: float = OVERLAP) -> dict:
    """Picks the clips of one golden video from its cached transcript (and reactions) and scores them."""
    name = video["id"]
    path = _resolve(storage.cache("transcripts"), video["transcript"])
    if path is None or not path.is_file():
        skipped = i18n.t("cli.eval_skipped", video=name, name=video["transcript"])
        log(skipped)
        return {"id": name, "error": skipped}
    transcript = read_json(path)
    signals_path = _resolve(storage.cache("signals"), video.get("signals"))
    reactions = Signals.from_json(read_json(signals_path)) if signals_path and signals_path.is_file() else None
    opts = {k: video.get(k) or v for k, v in DEFAULTS.items()}
    meter = MeteredLLM(llm)
    t0 = time.perf_counter()
    try:
        picks = find_highlights(transcript, meter, max_clips=int(opts["clips"]), min_len=float(opts["min_len"]),
                                max_len=float(opts["max_len"]), title=video.get("title") or "", signals=reactions,
                                speakers=_turns(_resolve(storage.cache("signals"), video.get("speakers"))))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:300]
        log(i18n.t("cli.eval_failed", video=name, error=error))
        return {"id": name, "error": error, "steps": meter.steps, "seconds": round(time.perf_counter() - t0, 3)}
    seconds = round(time.perf_counter() - t0, 3)
    words = [w for s in transcript.get("segments") or [] for w in s.get("words") or []]
    bounds = Boundaries(words) if words else None
    moments = video.get("moments") or []
    counts = score(picks, moments, bounds, threshold)
    llm_seconds = sum(s["seconds"] for s in meter.steps.values())
    out = {"id": name, "duration": transcript.get("duration"), "words": len(words), "seconds": seconds,
           "llm_calls": sum(s["calls"] for s in meter.steps.values()),
           "steps": {**meter.steps, "local": {"calls": 0, "seconds": round(max(0.0, seconds - llm_seconds), 3),
                                              "failed": 0}},
           "counts": counts, "metrics": rates(counts),
           "picks": [{"start": c.start, "end": c.end, "score": c.score, "hook": c.hook, "title": c.title,
                      "labels": [j for j, m in enumerate(moments)
                                 if overlap((c.start, c.end), (float(m["start"]), float(m["end"]))) >= threshold],
                      **((edges(c, bounds) or {}) if bounds else {})} for c in picks]}
    log(i18n.t("cli.eval_video", video=name, picks=len(picks), seconds=f"{seconds:.0f}", calls=out["llm_calls"]))
    return out


def summarize(videos: list[dict]) -> dict:
    """Every scored video's counts pooled (each label and each pick weighs the same), and the step costs of every
    video summed, failed calls of a failed video included."""
    done = [v for v in videos if "counts" in v]
    counts: dict[str, int] = {}
    steps: dict[str, dict] = {}
    for v in done:
        for k, n in v["counts"].items():
            counts[k] = counts.get(k, 0) + n
    for v in videos:
        for name, s in (v.get("steps") or {}).items():
            total = steps.setdefault(name, {"calls": 0, "seconds": 0.0, "failed": 0})
            for k in total:
                total[k] = round(total[k] + s.get(k, 0), 3)
    return {"videos": len(done), "skipped": len(videos) - len(done), "counts": counts, "metrics": rates(counts),
            "steps": steps, "seconds": round(sum(v["seconds"] for v in done), 3),
            "llm_calls": sum(v["llm_calls"] for v in done)}


def run(golden_path: Path, storage: Storage, llm, only: list[str] | None = None,
        threshold: float = OVERLAP) -> dict:
    golden = load_golden(golden_path)
    videos = [v for v in golden["videos"] if not only or str(v["id"]) in only]
    if not videos:
        raise ClippyError(key="error.eval_no_video", ids=", ".join(only or []))
    results = [run_video(v, storage, llm, threshold) for v in videos]
    return {"format": FORMAT, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "llm": llm.describe(), "think": think_enabled(), "golden": str(golden_path), "overlap": threshold,
            "videos": results, "summary": summarize(results)}


def _pct(value) -> str:
    return "–" if value is None else f"{100 * value:.0f}%"


COLUMNS = (("picks", "cli.eval_col_picks"), ("labels", "cli.eval_col_labels"), ("recall@1", "cli.eval_col_r1"),
           ("recall@3", "cli.eval_col_r3"), ("recall@5", "cli.eval_col_r5"), ("precision", "cli.eval_col_precision"),
           ("start_ok", "cli.eval_col_start"), ("end_ok", "cli.eval_col_end"), ("mid_cut", "cli.eval_col_midcut"),
           ("llm_calls", "cli.eval_col_calls"), ("seconds", "cli.eval_col_seconds"))


def _grid(rows: list[list[str]]) -> str:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(c.ljust(w) if i == 0 else c.rjust(w) for i, (c, w) in enumerate(zip(r, widths))) for r in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def _cells(v: dict) -> list[str]:
    counts, metrics = v.get("counts") or {}, v.get("metrics") or {}
    out = []
    for key, _ in COLUMNS:
        if key in RATES:
            out.append(_pct(metrics.get(key)))
        elif key == "seconds":
            out.append(f"{v.get('seconds', 0):.0f}")
        elif key == "llm_calls":
            out.append(str(v.get("llm_calls", 0)))
        else:
            out.append(str(counts.get(key, 0)))
    return out


def _steps_grid(steps: dict) -> str:
    rows = [[i18n.t("cli.eval_col_step"), i18n.t("cli.eval_col_calls"), i18n.t("cli.eval_col_seconds"),
             i18n.t("cli.eval_col_failed")]]
    for name in (*STEPS, "local"):
        if name in steps:
            s = steps[name]
            rows.append([i18n.t(f"cli.eval_step_{name}"), str(s["calls"]), f"{s['seconds']:.1f}", str(s["failed"])])
    return _grid(rows)


def table(report: dict) -> str:
    """The report as two plain-text tables: the scores per video with the pooled total, then the cost per step."""
    rows = [[i18n.t("cli.eval_col_video")] + [i18n.t(key) for _, key in COLUMNS]]
    for v in report["videos"]:
        rows.append([str(v["id"])] + (_cells(v) if "counts" in v else ["–"] * len(COLUMNS)))
    rows.append([i18n.t("cli.eval_total")] + _cells(report["summary"]))
    errors = [f"{v['id']}: {v['error']}" for v in report["videos"] if v.get("error")]
    return "\n\n".join([_grid(rows), _steps_grid(report["summary"]["steps"])] + (["\n".join(errors)] if errors else []))


def _delta(key: str, a, b) -> str:
    if a is None or b is None:
        return "–"
    if key in RATES:
        return f"{100 * (b - a):+.0f} pt"
    return f"{b - a:+.1f}" if isinstance(a, float) or isinstance(b, float) else f"{b - a:+d}"


def compare_table(a: dict, b: dict) -> str:
    """Two reports side by side: every pooled rate, the LLM calls and the seconds of each step."""
    sa, sb = a["summary"], b["summary"]
    rows = [[i18n.t("cli.eval_col_metric"), "A", "B", i18n.t("cli.eval_col_change")],
            [i18n.t("cli.eval_col_llm"), a.get("llm", ""), b.get("llm", ""), ""]]
    for key in RATES:
        x, y = sa["metrics"].get(key), sb["metrics"].get(key)
        rows.append([key, _pct(x), _pct(y), _delta(key, x, y)])
    for key in ("picks", "labels"):
        x, y = sa["counts"].get(key, 0), sb["counts"].get(key, 0)
        rows.append([key, str(x), str(y), _delta(key, x, y)])
    rows.append(["llm_calls", str(sa["llm_calls"]), str(sb["llm_calls"]), _delta("", sa["llm_calls"], sb["llm_calls"])])
    rows.append(["seconds", f"{sa['seconds']:.1f}", f"{sb['seconds']:.1f}", _delta("", sa["seconds"], sb["seconds"])])
    for name in (*STEPS, "local"):
        x, y = (sa["steps"].get(name) or {}).get("seconds"), (sb["steps"].get(name) or {}).get("seconds")
        if x is not None or y is not None:
            rows.append([f"{name} s", "–" if x is None else f"{x:.1f}", "–" if y is None else f"{y:.1f}",
                         _delta("", float(x or 0), float(y or 0))])
    return _grid(rows)


def load_report(path: Path) -> dict:
    try:
        report = read_json(path)
        if not isinstance(report["summary"]["metrics"], dict) or not isinstance(report["summary"]["steps"], dict):
            raise ValueError(path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ClippyError(key="error.eval_report", path=path) from exc
    return report


def _newest(folder: Path, pattern: str) -> str | None:
    found = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return found[0].name if found else None


def cached_inputs(storage: Storage, stem: str) -> dict | None:
    """The cache files of one video, by name: its newest aligned transcript (else its newest plain one), its
    reactions and its speaker turns; None when there is no transcript."""
    transcripts, signals = storage.cache("transcripts"), storage.cache("signals")
    transcript = _newest(transcripts, f"{stem}.*.aligned.json") or _newest(transcripts, f"{stem}.*.transcript.json")
    if transcript is None:
        return None
    reactions = next((f"{stem}.{k}.json" for k in SIGNAL_KINDS if (signals / f"{stem}.{k}.json").is_file()), None)
    return {"transcript": transcript, "signals": reactions, "speakers": _newest(signals, f"{stem}.speakers.*.json")}


def _stems(plan: dict, folder: Path) -> list[str]:
    source = str(plan.get("source") or "")
    tail = Path(urlparse(source).path).name if "://" in source else Path(source).stem
    return [s for s in dict.fromkeys((tail, folder.name, str(plan.get("title") or ""))) if s]


def bootstrap(storage: Storage, golden_path: Path) -> dict:
    """Adds every video with kept clips (data/projects/*/clips.json) and a cached transcript to the golden set, its
    clips as the labelled moments; videos already in it stay as they are, so hand edits are never lost."""
    golden = load_golden(golden_path) if golden_path.is_file() else {"format": FORMAT, "videos": []}
    known = {str(v["id"]) for v in golden["videos"]}
    added, kept, missing = 0, 0, []
    projects = storage.data / "projects"
    for plan_path in sorted(projects.glob("*/clips.json")) if projects.is_dir() else []:
        try:
            plan = read_json(plan_path)
            clips = [Clip.from_dict(c) for c in plan["clips"]]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        stems = _stems(plan, plan_path.parent)
        found = next(((s, inputs) for s in stems if (inputs := cached_inputs(storage, s))), None)
        if found is None:
            missing.append(plan_path.parent.name)
            continue
        stem, inputs = found
        if stem in known:
            kept += 1
            continue
        known.add(stem)
        golden["videos"].append({
            "id": stem, "title": plan.get("title") or stem, **inputs,
            "clips": max(DEFAULTS["clips"], len(clips)), "min_len": DEFAULTS["min_len"], "max_len": DEFAULTS["max_len"],
            "from": f"projects/{plan_path.parent.name}/clips.json",
            "moments": [{"start": round(c.start, 2), "end": round(c.end, 2), "grade": DEFAULT_GRADE,
                         "note": c.title or c.hook} for c in clips]})
        added += 1
    write_json(golden_path, golden)
    return {"added": added, "kept": kept, "missing": missing, "path": golden_path}
