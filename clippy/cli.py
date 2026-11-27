"""`clippy clip`: link/file in -> vertical clips with hooks and captions out; `clippy eval`: how good the picks are."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__, evaluation, i18n
from .captions import COLORS
from .concurrency import GuardedLLM
from .i18n import CAPTION_LANGUAGES
from .llm import BACKENDS, create_llm
from .pipeline import Options, Project
from .render import LAYOUTS
from .storage import Storage
from .tools import ROOT, ClippyError, fmt_ts, log, slugify, write_json


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clippy clip",
        description="Vertical (9:16) clips from a long video or podcast: LLM hook selection, speaker tracking, "
                    "animated captions. Local and free.",
    )
    p.add_argument("source", nargs="?", help="video link (YouTube, TikTok, Instagram, Twitch…) or a local video file")
    p.add_argument("-n", "--clips", type=int, default=5, help="how many clips (default: 5)")
    p.add_argument("--min", dest="min_len", type=float, default=20, help="minimum clip length in seconds (default: 20)")
    p.add_argument("--max", dest="max_len", type=float, default=90, help="maximum clip length in seconds; the topic sets the length up to it (default: 90)")
    p.add_argument("--wish", default="", help='what kind of moments to pick, e.g. "the funniest parts"')
    p.add_argument("--lang", default=None, choices=CAPTION_LANGUAGES,
                   help="spoken language of the video (default: detected; hu or en forces it)")
    p.add_argument("--captions", default=None, choices=CAPTION_LANGUAGES,
                   help="language of captions and opening text (default: the spoken one; another one is translated)")
    p.add_argument("--whisper", default="auto",
                   help="Whisper model: auto (GPU: large-v3, CPU: large-v3-turbo), large-v3, large-v3-turbo, small")
    p.add_argument("--vocab", default="",
                   help='names/slang to spell right, e.g. "DESH, Young Fly" (the video title is added automatically)')
    p.add_argument("--no-title-vocab", action="store_true", help="do not pass the video title to Whisper")
    p.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"], help="transcription device")
    p.add_argument("--llm", default=None, choices=list(BACKENDS),
                   help="hook picker: ollama (local), openrouter, claude (Claude subscription), codex (ChatGPT "
                        "subscription), openai (any compatible API), none (heuristic); default: the one set with "
                        "clippy llm")
    p.add_argument("--model", default=None, help="LLM model name (default: the one saved for that mode)")
    p.add_argument("--layout", default="auto", choices=LAYOUTS,
                   help="auto | face (speaker tracking) | center | blur (gameplay) | stream (facecam on top, game below)")
    p.add_argument("--facecam", default=None, help="stream layout: x,y,w,h in source pixels")
    p.add_argument("--cta", default="", help='closing call to action for the last 3 s, e.g. "Follow for part 2!"')
    p.add_argument("--hook", action="store_true", help="burn the suggested opening text over the first 3 seconds")
    p.add_argument("--highlight", default="yellow", choices=sorted(COLORS), help="colour of the highlighted word")
    p.add_argument("--no-upper", action="store_true", help="captions not in upper case")
    p.add_argument("--font", default="Arial Black", help="caption font (Windows font name)")
    p.add_argument("--no-tighten", action="store_true",
                   help="keep dead air and filler words inside clips (e.g. music or mood pieces)")
    p.add_argument("--punch", action="store_true", help="punch-in zoom on cuts")
    p.add_argument("--audio", default="voice", choices=["voice", "natural"],
                   help="voice: speech EQ + compressor (default) | natural: loudness normalisation only")
    p.add_argument("--isolate", default="auto", choices=["auto", "on", "off"],
                   help="isolate the voice from music/noise for transcription (Demucs; auto: when installed)")
    p.add_argument("--no-align", action="store_true", help="no forced word alignment")
    p.add_argument("--duck", type=float, default=0.0, help="lower music under speech by this many dB (e.g. 8); 0 = off")
    p.add_argument("--encoder", default="auto", choices=["auto", "nvenc", "x264"], help="video encoder (auto: NVENC on NVIDIA)")
    p.add_argument("--out", default=None, help="library folder for finished clips (default: CLIPPY_LIBRARY)")
    p.add_argument("--cookies-from-browser", default=None,
                   help="yt-dlp: browser cookies when the site asks for a sign-in (e.g. firefox; or CLIPPY_COOKIES_FROM_BROWSER)")
    p.add_argument("--only-select", action="store_true", help="only pick the clips (clips.json), edit it, then render")
    p.add_argument("--from-json", default=None, help="render from an (edited) clips.json")
    p.add_argument("--version", action="version", version=f"clippy {__version__}")
    return p


def build_eval_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clippy eval",
        description="How good the clip picks are: runs the picker on the cached transcripts of a golden set of good "
                    "moments, scores the picks against them and times every step. See docs/quality.md.",
    )
    p.add_argument("--bootstrap", action="store_true",
                   help="make or extend the golden set from the clips kept in data/projects, then edit it by hand")
    p.add_argument("--compare", nargs=2, metavar=("A", "B"), default=None,
                   help="two reports side by side: every rate, the LLM calls and the seconds of each step")
    p.add_argument("--golden", default=None, help="golden set file (default: data/eval/golden.json)")
    p.add_argument("--out", default=None, help="report file (default: data/eval/reports/<time>.json)")
    p.add_argument("--data", default=None, help="data folder to read the cache from (default: this checkout's data)")
    p.add_argument("--video", action="append", default=None, help="only this video of the golden set (repeatable)")
    p.add_argument("--overlap", type=float, default=evaluation.OVERLAP,
                   help="share of the shorter span a pick and a labelled moment must share to count as found (default: 0.5)")
    p.add_argument("--llm", default=None, choices=list(BACKENDS),
                   help="the AI to pick with (default: the one set with clippy llm)")
    p.add_argument("--model", default=None, help="LLM model name (default: the one saved for that mode)")
    return p


def eval_main(argv: list[str] | None = None) -> int:
    load_env()
    args = build_eval_parser().parse_args(argv)
    try:
        if args.compare:
            a, b = (evaluation.load_report(Path(p)) for p in args.compare)
            print(evaluation.compare_table(a, b))
            return 0
        storage = Storage(data=args.data) if args.data else Storage()
        golden = Path(args.golden) if args.golden else storage.golden_file
        if args.bootstrap:
            done = evaluation.bootstrap(storage, golden)
            log(i18n.t("cli.eval_bootstrap", added=done["added"], kept=done["kept"], missing=len(done["missing"]),
                       path=done["path"]))
            return 0
        llm = GuardedLLM(create_llm(args.llm, args.model, storage))
        report = evaluation.run(golden, storage, llm, only=args.video, threshold=args.overlap)
        out = Path(args.out) if args.out else storage.eval_report(time.strftime("%Y%m%d-%H%M%S"))
        write_json(out, report)
        print(evaluation.table(report))
        log(i18n.t("cli.eval_report", path=out))
        return 0
    except ClippyError as exc:
        log(f"\nERROR: {exc}")
        return 1
    except KeyboardInterrupt:
        log("\nInterrupted.")
        return 130


def facecam_box(text: str | None) -> tuple[int, int, int, int] | None:
    """The --facecam value as (x, y, w, h) source pixels."""
    if not text:
        return None
    try:
        box = tuple(int(v) for v in text.split(","))
    except ValueError:
        box = ()
    if len(box) != 4:
        raise ClippyError(key="error.facecam_format")
    return box


def options_from_args(args) -> Options:
    facecam = facecam_box(args.facecam)
    return Options(
        n=args.clips, min_len=args.min_len, max_len=args.max_len, instructions=args.wish, llm=args.llm,
        model=args.model, lang=args.lang, captions=args.captions, whisper=args.whisper, vocab=args.vocab,
        title_vocab=not args.no_title_vocab, device=args.device, isolate=args.isolate, align=not args.no_align,
        layout=args.layout, facecam=facecam, cta=args.cta, hook=args.hook, highlight=args.highlight,
        uppercase=not args.no_upper, font=args.font, tighten=not args.no_tighten, punch=args.punch,
        audio=args.audio, duck=args.duck, encoder=args.encoder, cookies_from_browser=args.cookies_from_browser,
    )


def load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass


def main(argv: list[str] | None = None) -> int:
    load_env()
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except ClippyError as exc:
        log(f"\nERROR: {exc}")
        return 1
    except KeyboardInterrupt:
        log("\nInterrupted.")
        return 130


def _run(args) -> int:
    t0 = time.time()
    opts = options_from_args(args)

    if args.from_json:
        source, clips, plan = Project.load_plan(Path(args.from_json))
        if not opts.lang:
            opts.lang = plan.get("language")
    elif args.source:
        source, clips = args.source, None
    else:
        raise ClippyError(key="error.no_source")

    storage = Storage(library=args.out)
    proj = Project(source, opts, storage).prepare()
    dest = storage.library_dir(proj.title)
    if clips is None:
        clips = proj.select()
        log(f"[plan] saved: {proj.save_plan(clips)}")
    else:
        log(f"[plan] {len(clips)} clips from {args.from_json}")

    for i, c in enumerate(clips, 1):
        log(f"  #{i}  {fmt_ts(c.start)}-{fmt_ts(c.end)}  ({c.duration:.0f} s)  score: {c.score:.0f}  hook: {c.hook}")
    if args.only_select:
        log(f"\nSelection done. Edit clips.json, then: clippy clip --from-json \"{storage.project(proj.title) / 'clips.json'}\"")
        return 0

    results = []
    for i, c in enumerate(clips, 1):
        name = f"clip_{i:02d}_{slugify(c.title or c.hook, 40)}"
        log(f"[render] {i}/{len(clips)}  {name}.mp4")
        if i < len(clips):
            proj.prefetch(clips[i])
        info = proj.render(c, dest, name)
        results.append({"file": f"{name}.mp4", **c.to_dict(), **info})

    write_json(storage.project(proj.title) / "render_report.json", results)
    log(f"\nDone: {len(results)} clips -> {dest}   ({(time.time() - t0) / 60:.1f} min)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
