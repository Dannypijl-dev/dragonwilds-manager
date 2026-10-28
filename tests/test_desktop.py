"""Desktop mode: the `app` command and the "panel already running" detection. No real window is opened."""
import http.server
import socket
import threading

import pytest
from waitress import create_server

from dwmanager import desktop
from dwmanager.__main__ import main


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def waitress_panel():
    """A real waitress server with the manager's ident, answering a redirect like the panel's / does."""
    def app(environ, start_response):
        start_response("302 Found", [("Location", "/login"), ("Content-Type", "text/plain")])
        return [b""]
    server = create_server(app, host="127.0.0.1", port=0, ident=desktop.SERVER_IDENT)
    threading.Thread(target=server.run, daemon=True).start()
    yield server.effective_port
    server.close()


@pytest.fixture
def other_server():
    """Something else listening on a port: a plain http.server with its own Server header."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"not the panel")

        def log_message(self, *a):
            pass
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_port
    httpd.shutdown()


def test_cli_parses_app(monkeypatch):
    calls = []
    monkeypatch.setattr(desktop, "run_app", lambda port, data_dir: calls.append((port, data_dir)) or 0)
    assert main(["app", "--port", "8099", "--data-dir", "x"]) == 0
    monkeypatch.setenv("DWM_PORT", "8123")
    assert main(["app"]) == 0
    assert calls == [(8099, "x"), (8123, None)]


def test_panel_running_detection(waitress_panel, other_server):
    assert desktop.panel_running(waitress_panel)
    assert not desktop.panel_running(other_server)
    assert not desktop.panel_running(free_port(), timeout=0.3)


def test_run_app_uses_running_panel(monkeypatch, waitress_panel):
    opened, started = [], []
    monkeypatch.setattr(desktop, "open_window", lambda url, on_closing=None: opened.append(url))
    monkeypatch.setattr(desktop.Embedded, "start", lambda self, *a, **k: started.append(self))
    monkeypatch.setitem(__import__("sys").modules, "webview", object())
    assert desktop.run_app(waitress_panel) == 0
    assert opened == [f"http://127.0.0.1:{waitress_panel}/"]
    assert started == []


def test_run_app_embedded_panel(monkeypatch, tmp_path):
    """No panel on the port: the manager starts in this process and stops when the window closes."""
    monkeypatch.setenv("DWM_FIREWALL", "none")
    monkeypatch.setitem(__import__("sys").modules, "webview", object())
    port = free_port()
    events = []

    def fake_window(url, on_closing=None):
        events.append(("open", url, desktop.panel_running(port)))
        # the closing handler lets the window close when no server runs
        assert on_closing(window=None) is True

    monkeypatch.setattr(desktop, "open_window", fake_window)
    assert desktop.run_app(port, tmp_path / "data") == 0
    assert events == [("open", f"http://127.0.0.1:{port}/", True)]
    assert not desktop.wait_until_running(port, seconds=3)  # shut down after the window closed
    assert (tmp_path / "data" / "manager.log").exists()


def test_confirm_close_asks_when_servers_run():
    class Inst:
        def __init__(self, running):
            self._running = running

        def running(self):
            return self._running

    class Window:
        def __init__(self, answer):
            self.answer, self.asked = answer, []

        def create_confirmation_dialog(self, title, text):
            self.asked.append((title, text))
            return self.answer

    class Mgr:
        settings = {"lang": "nl"}
        instances = {"a": Inst(True)}

    e = desktop.Embedded(1)
    assert e.confirm_close(Window(False)) is True  # no manager yet: nothing to stop
    e.manager = Mgr()
    w = Window(False)
    assert e.confirm_close(w) is False
    assert w.asked == [("Dragonwilds Manager sluiten?", desktop.i18n.STRINGS["nl"]["app_close_text"])]
    assert e.confirm_close(Window(True)) is True
    Mgr.instances = {"a": Inst(False)}
    assert e.confirm_close(Window(False)) is True
