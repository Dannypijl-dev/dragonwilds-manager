"""Command line: run the panel, or one of the small helpers the manager calls."""
import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from . import __version__, platforms


def main(argv=None):
    parser = argparse.ArgumentParser(prog="dragonwilds-manager", description="Dragonwilds Manager")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="run the web panel (default)")
    run.add_argument("--host", default=os.environ.get("DWM_HOST", "0.0.0.0"))
    run.add_argument("--port", type=int, default=int(os.environ.get("DWM_PORT", "8080")))
    run.add_argument("--data-dir", default=None)
    app = sub.add_parser("app", help="open the panel in its own window (desktop app)")
    app.add_argument("--port", type=int, default=int(os.environ.get("DWM_PORT", "8080")))
    app.add_argument("--data-dir", default=None)
    fw = sub.add_parser("firewall", help="(root helper) nftables block add/del, or ufw port open/close/status")
    fw.add_argument("action", choices=["add", "del", "port"])
    fw.add_argument("arg1", help="ip (add/del) or open|close|status (port)")
    fw.add_argument("port", type=int)
    fw.add_argument("minutes", type=int, nargs="?", default=0)
    cc = sub.add_parser("ctrl-c", help="(Windows helper) send Ctrl+C to a console process")
    cc.add_argument("pid", type=int)
    sub.add_parser("service", help="(Windows) run as a Windows service").add_argument("args", nargs="*")
    args = parser.parse_args(argv)

    if args.cmd == "firewall":
        from .firewall import nft_apply, ufw_port
        if args.action == "port":
            print(ufw_port(args.arg1, args.port))
        else:
            nft_apply(args.action, args.arg1, args.port, args.minutes or None)
        return 0
    if args.cmd == "ctrl-c":
        from .instance import send_ctrl_c
        return send_ctrl_c(args.pid)
    if args.cmd == "service":
        from .winservice import run_service
        return run_service(args.args)
    if args.cmd == "app":
        from .desktop import run_app
        return run_app(args.port, args.data_dir)
    return serve(getattr(args, "host", "0.0.0.0"), getattr(args, "port", 8080), getattr(args, "data_dir", None))


def serve(host, port, data_dir=None, ready=None, exit_on_stop=True, on_manager=None):
    """Run the panel until stopped. `ready(shutdown)` is called once the port is bound, with the function
    that stops everything cleanly; `on_manager(manager)` as soon as the Manager exists."""
    from waitress import create_server

    from .manager import Manager
    from .web import create_app

    data_dir = Path(data_dir or platforms.default_data_dir())
    data_dir.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(os.path.join(str(data_dir), "manager.log"), encoding="utf-8")]
    if sys.stderr is not None:  # a windowed exe has no console to log to
        handlers.insert(0, logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    log = logging.getLogger("dwmanager")
    log.info("Dragonwilds Manager %s, data in %s", __version__, data_dir)
    manager = Manager(data_dir)
    if on_manager:
        on_manager(manager)
    log.info("firewall mode for kicks: %s", manager.firewall.mode or "none")
    app = create_app(manager)
    manager.start_background()

    stopping = threading.Event()
    server = create_server(app, host=host, port=port, threads=8, max_request_body_size=8 * 1024**3,
                           ident="dragonwilds-manager")

    def shutdown(*_):
        if stopping.is_set():
            return
        stopping.set()
        log.info("shutting down, stopping servers")
        manager.shutdown()
        if exit_on_stop:
            os._exit(0)
        server.close()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, shutdown)
        except (ValueError, OSError):
            pass
    if ready:
        ready(shutdown)
    log.info("panel on http://%s:%s", host, port)
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
