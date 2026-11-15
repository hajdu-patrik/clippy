"""`clippy clip`: link/file in -> vertical clips with hooks and captions out."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__, i18n
from .captions import COLORS
from .pipeline import Options, Project
from .render import LAYOUTS
from .storage import Storage
from .tools import ROOT, ClippyError, fmt_ts, log, slugify, write_json


def build_parser(lang: str | None = None) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clippy clip",
        description="Vertical (9:16) clips from a long video or podcast: LLM hook selection, speaker tracking, "
                    "animated captions. Local and free.",
    )
    p.add_argument("source", nargs="?", help="video link (YouTube, TikTok, Instagram, Twitch…) or a local video file")
    p.add_argument("-n", "--clips", type=int, default=5, help="how many clips (default: 5)")
    p.add_argument("--min", dest="min_len", type=float, default=20, help="minimum clip length in seconds (default: 20)")
    p.add_argument("--max", dest="max_len", type=float, default=60, help="maximum clip length in seconds (default: 60)")
    p.add_argument("--wish", default="", help='what kind of moments to pick, e.g. "the funniest parts"')
    p.add_argument("--lang", default=None, choices=("hu", "en"), help="spoken language (default: detect hu/en)")
    p.add_argument("--captions", default=None, choices=("hu", "en"),
                   help="language of captions and opening text (default: the spoken one; another one is translated)")
    p.add_argument("--whisper", default="auto",
                   help="Whisper model: auto (GPU: large-v3, CPU: large-v3-turbo), large-v3, large-v3-turbo, small")
    p.add_argument("--vocab", default="",
                   help='names/slang to spell right, e.g. "DESH, Young Fly" (the video title is added automatically)')
    p.add_argument("--no-title-vocab", action="store_true", help="do not pass the video title to Whisper")
    p.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"], help="transcription device")
    p.add_argument("--llm", default=None, choices=["ollama", "openai", "none"],
                   help="hook picker: ollama (local), openai (Groq/Gemini/OpenRouter), none (heuristic)")
    p.add_argument("--model", default=None, help="LLM model name (default: CLIPPY_LLM_MODEL or clippy-qwen3:14b-q6k)")
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
    lang = lang or i18n.system_language()
    p.description = i18n.lookup(lang, "help.description") or p.description
    p._positionals.title = i18n.lookup(lang, "help.positionals") or p._positionals.title
    p._optionals.title = i18n.lookup(lang, "help.optionals") or p._optionals.title
    for action in p._actions:
        if action.dest not in ("help", "version"):
            action.help = i18n.lookup(lang, f"help.{action.dest}") or action.help
    return p


def options_from_args(args) -> Options:
    facecam = tuple(int(v) for v in args.facecam.split(",")) if args.facecam else None
    if facecam and len(facecam) != 4:
        raise ClippyError("--facecam format: x,y,w,h")
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
        raise ClippyError("Give a link or a file (or --from-json clips.json).")

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
