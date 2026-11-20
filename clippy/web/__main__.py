"""python -m clippy.web  ->  http://127.0.0.1:7860  (normally started through `clippy start`); `--lan` also listens on
the local network for paired phones and tablets."""

import argparse
import threading
import webbrowser

import uvicorn

from ..cli import load_env


def main() -> None:
    load_env()
    p = argparse.ArgumentParser(prog="clippy.web")
    p.add_argument("--port", type=int, default=7860)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--lan", action="store_true")
    args = p.parse_args()

    from ..storage import Storage
    from .lan import Lan
    from .server import create_app, quiet_closed_connections

    quiet_closed_connections()
    storage = Storage()
    lan = Lan(storage.web / "devices.json", args.port) if args.lan else None
    app = create_app(storage, lan=lan)
    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"Clippy is running: {url}   (stop: Ctrl+C or clippy down)")
    if lan is not None:
        print(f"Local network: {', '.join(f'http://{ip}:{args.port}' for ip in lan.addresses) or 'no private address'}")
    uvicorn.run(app, host="0.0.0.0" if lan else "127.0.0.1", port=args.port, log_level="warning")


main()
