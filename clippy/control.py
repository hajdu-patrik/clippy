"""`clippy`: the one command for everything, the web app (start / down / restart / status / open / logs),
command-line clipping (`clippy clip <link>`) and shell setup (`clippy install`)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import requests

from . import __version__, i18n
from .llm import OLLAMA_SERVER_ENV, loaded_models, ollama_running, unload_all
from .shell import COMMANDS, command_doc, flags_of
from .storage import DATA
from .tools import ROOT

PY = ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
STATE = DATA / "web"
PID_FILE = STATE / "server.pid"
LOG_FILE = STATE / "server.log"
PORT = int(os.environ.get("CLIPPY_PORT", "7860"))
URL = f"http://127.0.0.1:{PORT}"

_COLOR = bool((sys.stdout.isatty() or os.environ.get("FORCE_COLOR")) and not os.environ.get("NO_COLOR"))


def _rgb(r: int, g: int, b: int) -> str:
    return f"\033[38;2;{r};{g};{b}m" if _COLOR else ""


B = _rgb(217, 87, 122)
G = _rgb(79, 197, 143)
R = _rgb(255, 107, 107)
Y = _rgb(240, 190, 90)
D = "\033[2m" if _COLOR else ""
BOLD = "\033[1m" if _COLOR else ""
N = "\033[0m" if _COLOR else ""

_LETTERS = {
    "C": [" ██████╗", "██╔════╝", "██║     ", "██║     ", "╚██████╗", " ╚═════╝"],
    "L": ["██╗     ", "██║     ", "██║     ", "██║     ", "███████╗", "╚══════╝"],
    "I": ["██╗", "██║", "██║", "██║", "██║", "╚═╝"],
    "P": ["██████╗ ", "██╔══██╗", "██████╔╝", "██╔═══╝ ", "██║     ", "╚═╝     "],
    "Y": ["██╗   ██╗", "╚██╗ ██╔╝", " ╚████╔╝ ", "  ╚██╔╝  ", "   ██║   ", "   ╚═╝   "],
}
_GRADIENT = [(125, 22, 49), (158, 29, 62), (191, 44, 82), (217, 87, 122), (235, 141, 166), (246, 195, 209)]


def logo() -> str:
    rows = ["".join(_LETTERS[ch][i] for ch in "CLIPPY") for i in range(6)]
    return "\n".join(f"{_rgb(*_GRADIENT[i])}{row}{N}" for i, row in enumerate(rows))


def tr(key: str, **params) -> str:
    return i18n.t(key, **params)


def say(msg: str) -> None:
    print(f"{B}●{N} {msg}", flush=True)


def running() -> dict | None:
    """The web app's status, or None when it is not running."""
    try:
        return requests.get(f"{URL}/api/status", timeout=1.5).json()
    except (requests.RequestException, ValueError):
        return None


def _tasklist(pid: int) -> str:
    return subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True, text=True).stdout


def _pid_alive(pid: int) -> bool:
    return f'"{pid}"' in _tasklist(pid)


def _image_name(pid: int) -> str:
    out = _tasklist(pid)
    return out.split(",")[0].strip('"').lower() if out.startswith('"') else ""


def _port_pids() -> list[int]:
    """PIDs listening on our port (fallback when the pid file is missing, e.g. started by hand)."""
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True).stdout
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{PORT}") and parts[3].upper() == "LISTENING":
            pids.add(int(parts[4]))
    return sorted(pids)


def _tail(n: int = 40) -> None:
    if LOG_FILE.exists():
        print("\n".join(LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]))
    else:
        print(tr("cli.no_log"))


def _opera_gx() -> str | None:
    """Opera GX's launcher: from the registry (StartMenuInternet), else the newest versioned opera.exe."""
    try:
        import winreg

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"Software\Clients\StartMenuInternet\Opera GXStable\shell\open\command") as k:
                    cmd = winreg.QueryValue(k, None).strip()
                path = cmd.split('"')[1] if cmd.startswith('"') else cmd.split()[0]
                if Path(path).exists():
                    return path
            except OSError:
                continue
    except ImportError:
        pass
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Opera GX"
    if (base / "opera.exe").exists():
        return str(base / "opera.exe")
    versions = sorted(base.glob("*/opera.exe"), key=lambda p: [int(x) if x.isdigit() else 0 for x in p.parent.name.split(".")])
    return str(versions[-1]) if versions else None


def _detached(argv: list[str], **kw) -> subprocess.Popen:
    if sys.platform != "win32":
        return subprocess.Popen(argv, close_fds=True, start_new_session=True, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, **kw)
    return subprocess.Popen(argv, close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0), **kw)


def open_browser() -> None:
    """New tab in Opera GX (preferred), else a new Edge/Chrome window, else the default browser.
    CLIPPY_BROWSER=<path to exe> overrides."""
    custom = os.environ.get("CLIPPY_BROWSER")
    if sys.platform != "win32":
        if custom and Path(custom).exists():
            _detached([custom, URL])
        else:
            webbrowser.open(URL)
        return
    preferred = custom if custom and Path(custom).exists() else _opera_gx()
    if preferred:
        _detached([preferred, URL])
        return
    for exe, rel in (("msedge", r"Microsoft\Edge\Application\msedge.exe"), ("chrome", r"Google\Chrome\Application\chrome.exe")):
        path = shutil.which(exe) or next(
            (str(Path(b) / rel) for b in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""),
                                          os.environ.get("LOCALAPPDATA", "")) if b and (Path(b) / rel).exists()), None)
        if path:
            _detached([path, "--new-window", URL])
            return
    os.startfile(URL)


def ensure_ollama() -> None:
    if ollama_running():
        return
    if sys.platform == "win32":
        app = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama app.exe"
        argv = [str(app)] if app.exists() else None
    else:
        found = shutil.which("ollama")
        argv = [found, "serve"] if found else None
    if argv is None:
        say(f"{Y}{tr('cli.ollama_missing')}{N}")
        return
    say(tr("cli.ollama_starting"))
    _detached(argv, env={**os.environ, **OLLAMA_SERVER_ENV})
    for _ in range(30):
        if ollama_running():
            return
        time.sleep(1)
    say(f"{Y}{tr('cli.ollama_slow')}{N}")


def _spawn_server(log, lan: bool = False) -> subprocess.Popen:
    """A hidden console of its own (no window, inherited by ffmpeg and the other children), outside the
    terminal's process group and job, so closing that terminal never stops the server."""
    argv = [str(PY), "-m", "clippy.web", "--no-browser", "--port", str(PORT), *(["--lan"] if lan else [])]
    kwargs = dict(cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, close_fds=True,
                  env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
    if sys.platform != "win32":
        return subprocess.Popen(argv, start_new_session=True, **kwargs)
    hidden = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        return subprocess.Popen(argv, creationflags=hidden | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs)
    except OSError:
        return subprocess.Popen(argv, creationflags=hidden, **kwargs)


def lan_wanted(args: list[str]) -> bool:
    return "--lan" in args or os.environ.get("CLIPPY_LAN", "").strip().lower() in ("1", "true", "yes", "on")


def _post(path: str) -> dict | None:
    try:
        r = requests.post(f"{URL}{path}", headers={"X-Clippy-Request": "1"}, timeout=5)
        return r.json() if r.ok else None
    except (requests.RequestException, ValueError):
        return None


def _qr(url: str) -> str:
    try:
        import segno
    except ImportError:
        return ""
    from io import StringIO

    out = StringIO()
    segno.make(url, error="m").terminal(out=out, compact=True, border=2)
    return out.getvalue()


def pair(reset: bool = False) -> int:
    """A one-time link (and QR code) that lets a phone or tablet on the same Wi-Fi in; `reset` forgets every device."""
    st = running()
    if not st or not st.get("lan"):
        say(f"{Y}{tr('cli.lan_off')}{N}")
        return 1
    if reset:
        done = _post("/api/lan/forget")
        say(tr("cli.lan_forgotten", n=(done or {}).get("forgotten", 0)))
        return 0 if done else 1
    code = _post("/api/lan/pair")
    if not code:
        say(f"{R}{tr('cli.lan_failed')}{N}")
        return 1
    qr = _qr(code["url"])
    print(qr, end="")
    say(tr("cli.lan_pair", minutes=code["minutes"]))
    print(f"  {BOLD}{code['url']}{N}")
    if not qr:
        print(f"{Y}{tr('cli.lan_no_qr')}{N}")
    print(f"{D}{tr('cli.lan_firewall')}{N}")
    return 0


def start(open_ui: bool = True, lan: bool = False) -> int:
    if (st := running()):
        say(tr("cli.already_running", url=URL))
        if lan and not st.get("lan"):
            say(f"{Y}{tr('cli.lan_restart')}{N}")
        if open_ui:
            open_browser()
        return 0
    if not PY.exists():
        print(f"{R}{tr('cli.no_venv', path=PY)}{N}")
        return 1
    ensure_ollama()
    STATE.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "a", encoding="utf-8")
    log.write(f"\n===== start {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    log.flush()
    proc = _spawn_server(log, lan)
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    say(tr("cli.starting"))
    for _ in range(60):
        if running():
            say(f"{G}{tr('cli.started', url=URL)}{N}")
            if lan:
                pair()
            if open_ui:
                open_browser()
            return 0
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    print(f"{R}{tr('cli.could_not_start', path=LOG_FILE)}{N}")
    _tail(20)
    return 1


def down(stop_ollama: bool = False) -> int:
    pids: list[int] = []
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            if _pid_alive(pid):
                pids.append(pid)
        except ValueError:
            pass
    for pid in _port_pids():
        if pid not in pids and _image_name(pid).startswith("python"):
            pids.append(pid)
    if not pids and not running():
        say(tr("cli.not_running"))
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    PID_FILE.unlink(missing_ok=True)
    if pids:
        for _ in range(20):
            if not running():
                break
            time.sleep(0.25)
        say(f"{G}{tr('cli.stopped')}{N}")
    for name in unload_all():
        say(tr("cli.vram_freed", models=name))
    if stop_ollama:
        subprocess.run(["taskkill", "/IM", "ollama app.exe", "/F"], capture_output=True)
        subprocess.run(["taskkill", "/IM", "ollama.exe", "/F"], capture_output=True)
        say(tr("cli.ollama_stopped"))
    return 0


def status_line() -> str:
    st = running()
    if not st:
        return f"{D}○{N} {tr('cli.status_off')}  {D}·{N}  {tr('cli.status_start')}: {B}clippy start{N}"
    if st.get("busy"):
        current = st["current"]
        label = i18n.t(current["label_t"]["key"], **current["label_t"]["params"]) if current.get("label_t") \
            else current["label"]
        queued = tr("cli.status_queued", n=len(st["queue"])) if st.get("queue") else ""
        others = len(st.get("running") or []) - 1
        queued = (tr("cli.status_parallel", n=others) if others > 0 else "") + queued
        percent = f" · {current['percent']:.0f}%" if current.get("percent") is not None else ""
        work = f"{Y}{tr('cli.status_busy', label=label)}{N}{percent}{queued}"
    else:
        work = tr("cli.status_idle")
    models = loaded_models()
    if models is None:
        llm = f"{Y}{tr('cli.ollama_off')}{N}"
    else:
        llm = "LLM: " + (", ".join(f"{m['name']} ({m.get('size_vram', 0) / 2**30:.1f} GiB)" for m in models)
                         or tr("cli.llm_standby"))
    lan = f"  {D}·{N}  {tr('cli.status_lan')}" if st.get("lan") else ""
    return f"{G}●{N} {tr('cli.running')}  {D}·{N}  {URL}{lan}  {D}·{N}  {work}  {D}·{N}  {llm}"


def wait_until_idle(poll: float = 2.0) -> bool:
    """A restart waits for running and queued jobs (they would start again anyway, but from the beginning)."""
    shown = False
    try:
        while (st := running()) and (st.get("busy") or st.get("queue")):
            if not shown:
                say(f"{Y}{tr('cli.restart_wait')}{N}")
                shown = True
            time.sleep(poll)
    except KeyboardInterrupt:
        say(tr("cli.restart_cancelled"))
        return False
    return True


def status() -> int:
    print(status_line())
    return 0 if running() else 1


def banner() -> None:
    print()
    print(logo())
    print(f"{tr('cli.welcome')} {BOLD}Clippy {__version__}{N}: {tr('cli.tagline')}")
    print(status_line())
    print()
    print(f"{BOLD}{tr('cli.usage')}:{N} clippy <command> [options]")
    print()
    print(f"{BOLD}{tr('cli.commands')}:{N}")
    for name in COMMANDS:
        print(f"  {B}{name:<9}{N} {command_doc(name)}")
    print()
    print(f"{BOLD}{tr('cli.options')}:{N}")
    for name in ("start", "down"):
        for flag, doc, _ in flags_of(name):
            print(f"  {B}{flag:<14}{N} {name}: {doc}")
    print(f"  {B}{'clip -h':<14}{N} {tr('cli.clip_options')}")
    print()
    print(f"{D}{tr('cli.footer', url=URL)}{N}")
    print()


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        os.system("")
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = (args[0].lower() if args else "help").lstrip("-")
    if cmd == "clip":
        from .cli import main as clip_main

        return clip_main(args[1:])
    if cmd == "install":
        from .shell import install

        return install(say=say)
    if cmd in ("start", "up", "run"):
        return start(open_ui="--no-browser" not in args, lan=lan_wanted(args))
    if cmd in ("down", "stop", "kill"):
        return down(stop_ollama="--all" in args)
    if cmd == "restart":
        lan = lan_wanted(args) or bool((running() or {}).get("lan"))
        if "--force" not in args and not wait_until_idle():
            return 1
        down()
        time.sleep(1)
        return start(open_ui="--no-browser" not in args, lan=lan)
    if cmd == "pair":
        return pair(reset="--reset" in args)
    if cmd == "status":
        return status()
    if cmd == "open":
        if not running():
            return start()
        open_browser()
        return 0
    if cmd in ("logs", "log"):
        _tail(60)
        return 0
    if cmd not in ("help", "h"):
        print(f"{R}{tr('cli.unknown', cmd=args[0])}{N}")
    banner()
    return 0 if cmd in ("help", "h") else 2
