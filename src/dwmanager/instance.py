"""One Dragonwilds server: its folder, settings, process and log."""
import logging
import os
import re
import secrets
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import psutil

from . import firewall, mods, platforms, steamcmd, world_settings, worlds
from .history import History
from .logwatch import LogWatcher
from .serverfiles import ServerFiles
from .util import read_json, write_json

log = logging.getLogger("dwmanager.instance")

DEFAULTS = {
    "name": "Dragonwilds server",
    "port": 7777,
    "auto_start": True,       # start when the manager starts
    "auto_restart": True,     # start again after a crash
    "update_on_start": True,  # SteamCMD update before every start, so the server matches the game client
    "backup_keep": 14,
    "block_minutes": 1440,    # whitelist: how long a refused player's IP stays blocked
    "schedules": [],
}
STOP_TIMEOUT = 120
CRASH_WINDOW, CRASH_LIMIT = 600, 4  # more crashes than this in 10 minutes: stop restarting
RESTART_DELAY = 30


class InstanceBusy(RuntimeError):
    pass


class Instance:
    def __init__(self, root, firewall_, steamcmd_dir):
        self.root = Path(root)
        self.id = self.root.name
        self.firewall = firewall_
        self.steamcmd_dir = steamcmd_dir
        self.config_file = self.root / "instance.json"
        self.config = {**DEFAULTS, **read_json(self.config_file, {})}
        self.files = ServerFiles(self.root / "server", self.root / "backups", self.config["backup_keep"])
        self.history = History(self.root / "history.json")
        self.watcher = LogWatcher(self.files.log_file)
        self.lock = threading.RLock()     # one operation at a time (start/stop/backup/settings)
        self.proc = None                  # psutil.Process of the running server
        self.popen = None
        self._task_token = None
        self.task = None                  # long operation in progress: "installing", "updating", "stopping", ...
        self.progress = None
        self.message = None               # last error or note for the UI
        self.started_at = None
        self.intended_running = False
        self.crashes = []
        self.restart_at = None
        self._adopt()

    # ---------- folders ----------
    @property
    def server_dir(self):
        return self.files.server_dir

    @property
    def mods_disabled_dir(self):
        return self.root / "mods-disabled"

    @property
    def port(self):
        return int(self.config["port"])

    @property
    def installed(self):
        return platforms.server_executable(self.server_dir) is not None

    def save_config(self):
        self.files.keep = int(self.config["backup_keep"])
        write_json(self.config_file, self.config)

    # ---------- state ----------
    def running(self):
        p = self.proc
        try:
            return p is not None and p.is_running() and p.status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False

    def state(self):
        if self.task:
            return self.task
        if not self.installed:
            return "not_installed"
        if self.running():
            return "running" if self.watcher.ready else "starting"
        if self.restart_at:
            return "crashed"
        return "stopped"

    def memory(self):
        try:
            return sum(p.memory_info().rss for p in [self.proc, *self.proc.children(recursive=True)])
        except (psutil.Error, AttributeError):
            return None

    # ---------- install / update ----------
    def _background(self, name, func):
        if self.task:
            raise InstanceBusy(self.task)
        token = object()
        self.task, self._task_token = name, token
        self.message = None

        def run():
            try:
                func()
            except Exception as exc:
                log.exception("%s: %s failed", self.id, name)
                self.message = f"{name}: {exc}"
            finally:
                if self._task_token is token:  # a follow-up task (e.g. update after stop) may have taken over
                    self.task, self.progress = None, None

        threading.Thread(target=run, daemon=True, name=f"{self.id}-{name}").start()

    def _progress(self, stage, percent):
        self.progress = None if percent is None else round(percent, 1)

    def install(self, then_start=False):
        def work():
            steamcmd.install_server(self.steamcmd_dir, self.server_dir, self._progress)
            if not self.installed:
                raise RuntimeError("SteamCMD finished but the server binary is missing")
            pending = self.config.pop("initial_ini", None)
            if pending:
                self.files.write_ini(pending)
                self.save_config()
            if then_start and self.files.read_ini()[0].get("OwnerId"):
                self.task = None
                self.start()
        self._background("installing", work)

    def update(self, start_after=None):
        """Stop (if running), update through SteamCMD, start again if it was running."""
        was_running = self.running() if start_after is None else start_after

        def work():
            self._stop_now()
            steamcmd.install_server(self.steamcmd_dir, self.server_dir, self._progress)
            if was_running:
                self.task = None
                self._launch()
        self._background("updating", work)

    # ---------- start / stop ----------
    def start(self):
        if self.running():
            return
        if not self.installed:
            raise RuntimeError("server_not_installed")
        if not self.files.read_ini()[0].get("OwnerId"):
            raise RuntimeError("owner_missing")
        self.restart_at = None
        if self.config.get("update_on_start"):
            def work():
                steamcmd.install_server(self.steamcmd_dir, self.server_dir, self._progress)
                self.task = None
                self._launch()
            self._background("updating", work)
        else:
            self._launch()

    def _launch(self):
        with self.lock:
            if self.running():
                return
            cmd = platforms.server_command(self.server_dir, self.port)
            if not cmd:
                raise RuntimeError("server_not_installed")
            firewall.allow_game_port(self.port, self.id)
            (self.root / "logs").mkdir(exist_ok=True)
            out = open(self.root / "logs/stdout.log", "ab")
            kwargs = {"cwd": str(self.server_dir), "stdin": subprocess.DEVNULL, "stdout": out, "stderr": subprocess.STDOUT}
            if platforms.WINDOWS:
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            else:
                kwargs["start_new_session"] = True  # a Ctrl+C in the manager's terminal must not hit the server
            self.watcher.reset_run()
            self.popen = subprocess.Popen(cmd, **kwargs)
            out.close()
            self.proc = psutil.Process(self.popen.pid)
            self.started_at = datetime.now()
            self.intended_running = True
            write_json(self.root / "runtime.json", {"pid": self.popen.pid, "create_time": self.proc.create_time(),
                                                    "started_at": self.started_at.isoformat()})
            log.info("%s: started pid %s on port %s", self.id, self.popen.pid, self.port)

    def stop(self):
        """Stop and wait (up to STOP_TIMEOUT). Blocking; the web layer runs it in the background."""
        self.restart_at = None
        self._stop_now()

    def _stop_now(self):
        with self.lock:
            self.intended_running = False
            p = self.proc
            if p is None or not self.running():
                self.proc = None
                return
            log.info("%s: stopping pid %s", self.id, p.pid)
            _graceful_stop(p)
            try:
                p.wait(STOP_TIMEOUT)
            except psutil.TimeoutExpired:
                log.warning("%s: did not stop in %ss, killing", self.id, STOP_TIMEOUT)
                for child in p.children(recursive=True):
                    child.kill()
                p.kill()
                p.wait(30)
            if self.popen:
                self.popen.wait()
            self.proc, self.popen, self.started_at = None, None, None
            (self.root / "runtime.json").unlink(missing_ok=True)

    def stop_async(self):
        self._background("stopping", self.stop)

    def restart_async(self):
        def work():
            self.stop()
            self.task = None
            self.start()
        self._background("stopping", work)

    def with_stopped(self, change, backup=False):
        """Stop if running, optionally back up, apply change(), start again. Returns whether it restarts."""
        if self.task:
            raise InstanceBusy(self.task)
        if self.running() and not self.watcher.ready:
            raise InstanceBusy("starting")
        with self.lock:
            was_running = self.running()
            self._stop_now()
            try:
                if backup:
                    self.files.make_backup("auto")
                change()
            finally:
                if was_running:
                    self._launch()
        return was_running

    # ---------- supervision (called every few seconds by the manager) ----------
    def tick(self):
        if self.proc is not None and not self.running():
            code = self.popen.poll() if self.popen else None
            self.proc, self.popen = None, None
            (self.root / "runtime.json").unlink(missing_ok=True)
            if self.intended_running:
                now = time.time()
                self.crashes = [t for t in self.crashes if now - t < CRASH_WINDOW] + [now]
                log.warning("%s: server exited unexpectedly (code %s)", self.id, code)
                if self.config["auto_restart"] and len(self.crashes) <= CRASH_LIMIT:
                    self.restart_at = now + RESTART_DELAY
                    self.message = f"crashed (exit code {code}), restarting in {RESTART_DELAY}s"
                else:
                    self.intended_running = False
                    self.message = f"crashed (exit code {code}); too many crashes, not restarting"
        if self.restart_at and time.time() >= self.restart_at and not self.task:
            self.restart_at = None
            try:
                self.start()
            except Exception as exc:
                self.message = str(exc)
        return self.watcher.poll()

    def _adopt(self):
        """After a manager restart: pick up a server process that is still running."""
        rt = read_json(self.root / "runtime.json", None)
        if not rt:
            return
        try:
            p = psutil.Process(rt["pid"])
            if abs(p.create_time() - rt["create_time"]) < 2 and "RSDragonwilds" in " ".join(p.cmdline()):
                self.proc, self.intended_running = p, True
                self.started_at = datetime.fromisoformat(rt["started_at"])
                log.info("%s: adopted running server pid %s", self.id, p.pid)
                return
        except (psutil.Error, KeyError, ValueError):
            pass
        (self.root / "runtime.json").unlink(missing_ok=True)

    # ---------- worlds / mods helpers ----------
    def world_info(self):
        name = self.files.read_ini()[0].get("DefaultWorldName", "")
        return world_settings.read(self.files.saves_dir / f"{name}.sav") if name else None

    def list_worlds(self):
        return worlds.list_worlds(self.files.saves_dir, self.files.read_ini()[0].get("DefaultWorldName", ""))

    def list_mods(self):
        return mods.list_mods(self.files.mods_dir, self.mods_disabled_dir)

    def remove_files(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)


def _graceful_stop(p):
    """Ask the server to shut down cleanly so it saves the world: SIGINT on Linux, Ctrl+C on Windows."""
    try:
        if platforms.WINDOWS:
            subprocess.run(platforms.self_command("ctrl-c", str(p.pid)), timeout=20,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.kill(p.pid, signal.SIGINT)
    except Exception:
        log.exception("graceful stop of pid %s failed", p.pid)


def send_ctrl_c(pid):
    """Windows helper (run as its own process): attach to the server's console and send Ctrl+C."""
    import ctypes
    k = ctypes.windll.kernel32
    k.FreeConsole()
    if not k.AttachConsole(int(pid)):
        return 1
    k.SetConsoleCtrlHandler(None, True)  # don't stop ourselves
    ok = k.GenerateConsoleCtrlEvent(0, 0)  # CTRL_C_EVENT to every process on that console
    time.sleep(2)
    return 0 if ok else 2


def new_id(name, existing):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "server"
    candidate = base
    while candidate in existing:
        candidate = f"{base}-{secrets.token_hex(2)}"
    return candidate
