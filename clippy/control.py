"""`clippy`: the one command for everything, the web app (start / down / restart / status / open / logs),
command-line clipping (`clippy clip <link>`) and shell setup (`clippy install`)."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path

import requests

from . import __version__, i18n
from .llm import OLLAMA_SERVER_ENV, create_llm, loaded_models, mode_of, ollama_running, stored, unload_all
from .shell import COMMANDS, command_doc, flags_of
from .storage import DATA
from .tools import ROOT, ClippyError

PY = ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
STATE = DATA / "web"
PID_FILE = STATE / "server.pid"
LOG_FILE = STATE / "server.log"
PORT = int(os.environ.get("CLIPPY_PORT", "7860"))
URL = f"http://127.0.0.1:{PORT}"
SYSTEM_TIMEOUT = 30

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


def _system(argv: list[str]) -> str:
    """The output of a system tool, or "" when it is missing or hangs."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=SYSTEM_TIMEOUT).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


class WindowsProcesses:
    """The server's processes found and stopped with tasklist, netstat and taskkill."""

    @staticmethod
    def _tasklist(pid: int) -> str:
        return _system(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"])

    def alive(self, pid: int) -> bool:
        return f'"{pid}"' in self._tasklist(pid)

    def name(self, pid: int) -> str:
        out = self._tasklist(pid)
        return out.split(",")[0].strip('"').lower() if out.startswith('"') else ""

    def listening(self, port: int) -> list[int]:
        pids = set()
        for line in _system(["netstat", "-ano", "-p", "TCP"]).splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
                pids.add(int(parts[4]))
        return sorted(pids)

    def kill_tree(self, pid: int) -> None:
        _system(["taskkill", "/PID", str(pid), "/T", "/F"])

    def kill_ollama(self) -> None:
        for image in ("ollama app.exe", "ollama.exe"):
            _system(["taskkill", "/IM", image, "/F"])


class PosixProcesses:
    """The same on macOS and Linux: signals, ps, lsof and pkill; the server leads its own process group."""

    def alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def name(self, pid: int) -> str:
        return Path(_system(["ps", "-p", str(pid), "-o", "comm="]).strip()).name.lower()

    def listening(self, port: int) -> list[int]:
        out = _system(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"])
        return sorted({int(pid) for pid in out.split() if pid.isdigit()})

    def kill_tree(self, pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

    def kill_ollama(self) -> None:
        _system(["pkill", "-x", "ollama"])


PROCESSES = WindowsProcesses() if sys.platform == "win32" else PosixProcesses()


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
    if mode_of(stored()) == "ollama":
        ensure_ollama()
    STATE.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as log:
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
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            if PROCESSES.alive(pid):
                pids.append(pid)
        except ValueError:
            pass
    for pid in PROCESSES.listening(PORT):
        if pid not in pids and PROCESSES.name(pid).startswith("python"):
            pids.append(pid)
    if not pids and not running():
        say(tr("cli.not_running"))
    for pid in pids:
        PROCESSES.kill_tree(pid)
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
        PROCESSES.kill_ollama()
        say(tr("cli.ollama_stopped"))
    return 0


def _llm_text() -> str:
    """The AI in the status line: the models Ollama holds in VRAM, or the mode that answers instead."""
    if mode_of(stored()) != "ollama":
        try:
            return "LLM: " + create_llm().describe()
        except ClippyError as exc:
            return f"{Y}{exc}{N}"
    models = loaded_models()
    if models is None:
        return f"{Y}{tr('cli.ollama_off')}{N}"
    return "LLM: " + (", ".join(f"{m['name']} ({m.get('size_vram', 0) / 2**30:.1f} GiB)" for m in models)
                      or tr("cli.llm_standby"))


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
    llm = _llm_text()
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


def _restart(args: list[str]) -> int:
    lan = lan_wanted(args) or bool((running() or {}).get("lan"))
    if "--force" not in args and not wait_until_idle():
        return 1
    down()
    time.sleep(1)
    return start(open_ui="--no-browser" not in args, lan=lan)


def _open(args: list[str]) -> int:
    if not running():
        return start()
    open_browser()
    return 0


def _logs(args: list[str]) -> int:
    _tail(60)
    return 0


def _clip(args: list[str]) -> int:
    from .cli import main as clip_main

    return clip_main(args)


def _install(args: list[str]) -> int:
    from .llm_setup import first_run
    from .shell import install

    code = install(say=say)
    if code == 0:
        first_run(say)
    return code


def _llm(args: list[str]) -> int:
    from .llm_setup import command

    return command(args, say)


def _help(args: list[str]) -> int:
    banner()
    return 0


ACTIONS: dict[str, Callable[[list[str]], int]] = {
    "start": lambda args: start(open_ui="--no-browser" not in args, lan=lan_wanted(args)),
    "down": lambda args: down(stop_ollama="--all" in args),
    "restart": _restart,
    "status": lambda args: status(),
    "open": _open,
    "pair": lambda args: pair(reset="--reset" in args),
    "logs": _logs,
    "clip": _clip,
    "llm": _llm,
    "install": _install,
    "help": _help,
}
ALIASES = {"up": "start", "run": "start", "stop": "down", "kill": "down", "log": "logs", "h": "help"}


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        os.system("")
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = list(sys.argv[1:] if argv is None else argv)
    name = (args[0].lower() if args else "help").lstrip("-")
    action = ACTIONS.get(ALIASES.get(name, name))
    if action is None:
        print(f"{R}{tr('cli.unknown', cmd=args[0])}{N}")
        banner()
        return 2
    return action(args[1:])
