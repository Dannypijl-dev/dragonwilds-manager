"""Desktop mode: the panel in a native window (pywebview) instead of a browser tab.

  dragonwilds-manager app [--port N] [--data-dir DIR]

When the panel already answers on the port (the Windows service, say) the window simply shows it.
Otherwise the manager runs inside this process, and closing the window stops it: the servers save
their worlds first, like a service stop.
"""
import logging
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from . import i18n, platforms
from .util import read_json

TITLE = "Dragonwilds Manager"
ICON = Path(__file__).parent / "static" / "dragonwilds-manager.ico"
SERVER_IDENT = "dragonwilds-manager"
WINDOW_SIZE = (1280, 800)
log = logging.getLogger("dwmanager.desktop")


def panel_url(port):
    return f"http://127.0.0.1:{port}/"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def panel_running(port, timeout=1.0):
    """True when something that calls itself this manager answers on the port (a 302 to /login counts)."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(panel_url(port), timeout=timeout) as response:
            server = response.headers.get("Server", "")
    except urllib.error.HTTPError as err:
        server = err.headers.get("Server", "")
    except (OSError, ValueError):
        return False
    return server.startswith(SERVER_IDENT)


def wait_until_running(port, seconds=30.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if panel_running(port, timeout=0.5):
            return True
        time.sleep(0.2)
    return False


def _serve(*args, **kwargs):
    from .__main__ import serve
    return serve(*args, **kwargs)


def _lang(data_dir):
    """The panel's language, read from the manager settings (the Manager object may not exist yet)."""
    data_dir = Path(data_dir or platforms.default_data_dir())
    return read_json(data_dir / "manager.json", {}).get("lang", "en")


class Embedded:
    """The manager running inside this process, in a background thread."""

    def __init__(self, port, data_dir=None):
        self.port = port
        self.data_dir = data_dir
        self.manager = None
        self.shutdown = None
        self.error = None
        self._ready = threading.Event()

    def start(self, serve=_serve, timeout=60.0):
        def ready(shutdown):
            self.shutdown = shutdown
            self._ready.set()

        def on_manager(manager):
            self.manager = manager

        def run():
            try:
                serve("127.0.0.1", self.port, self.data_dir, ready=ready, exit_on_stop=False, on_manager=on_manager)
            except BaseException as exc:  # noqa: BLE001 - reported to the caller below
                self.error = exc
            finally:
                self._ready.set()

        threading.Thread(target=run, name="panel", daemon=True).start()
        self._ready.wait(timeout)
        if self.error:
            raise RuntimeError(f"the panel could not start: {self.error}") from self.error
        if self.shutdown is None or not wait_until_running(self.port, timeout):
            raise RuntimeError(f"the panel did not answer on port {self.port}")
        return self

    def servers_running(self):
        return self.manager is not None and any(i.running() for i in self.manager.instances.values())

    def confirm_close(self, window):
        """Closing handler for the window: ask first when game servers would be stopped by it."""
        if not self.servers_running():
            return True
        t = i18n.translator(self.manager.settings.get("lang", "en"))
        return bool(window.create_confirmation_dialog(t("app_close_title"), t("app_close_text")))

    def stop(self):
        if self.shutdown:
            self.shutdown()


def open_window(url, on_closing=None):
    """Show the panel in a native window; returns when the window is closed."""
    import webview

    window = webview.create_window(TITLE, url, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1],
                                   min_size=(900, 600), text_select=True)
    if on_closing:
        def closing(window):
            return on_closing(window)
        window.events.closing += closing
    webview.start(icon=str(ICON) if ICON.is_file() else None)


def _say(text):
    if sys.stderr is not None:
        print(text, file=sys.stderr)


def browser_fallback(port, data_dir=None):
    """No pywebview: open the default browser instead, running the panel here if it isn't up yet."""
    _say(i18n.translator(_lang(data_dir))("app_no_webview"))
    url = panel_url(port)
    if panel_running(port):
        webbrowser.open(url)
        return 0

    def ready(_shutdown):
        def when_up():
            if wait_until_running(port):
                webbrowser.open(url)
        threading.Thread(target=when_up, daemon=True).start()

    return _serve("127.0.0.1", port, data_dir, ready=ready)


def run_app(port, data_dir=None):
    try:
        import webview  # noqa: F401
    except ImportError:
        return browser_fallback(port, data_dir)
    url = panel_url(port)
    if panel_running(port):
        open_window(url)
        return 0
    embedded = Embedded(port, data_dir).start()
    try:
        open_window(url, on_closing=embedded.confirm_close)
    finally:
        embedded.stop()
    return 0
