"""The manager: all server instances, its own settings and the background loops."""
import logging
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

from . import schedule
from .firewall import Firewall
from .instance import Instance, InstanceBusy, new_id
from .util import read_json, write_json

log = logging.getLogger("dwmanager")
FIRST_PORT = 7777


class Manager:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings_file = self.data_dir / "manager.json"
        self.settings = read_json(self.settings_file, {})
        if "secret_key" not in self.settings:
            self.settings["secret_key"] = secrets.token_hex(32)
            self.save_settings()
        self.steamcmd_dir = self.data_dir / "steamcmd"
        self.firewall = Firewall(self.data_dir / "blocks.json")
        self.instances = {}
        servers = self.data_dir / "servers"
        servers.mkdir(exist_ok=True)
        for d in sorted(servers.iterdir()):
            if (d / "instance.json").exists():
                self.instances[d.name] = Instance(d, self.firewall, self.steamcmd_dir)
        self._stop = threading.Event()

    def save_settings(self):
        write_json(self.settings_file, self.settings)

    # ---------- instances ----------
    def get(self, iid):
        inst = self.instances.get(iid)
        if inst is None:
            raise KeyError(iid)
        return inst

    def free_port(self):
        used = {i.port for i in self.instances.values()}
        port = FIRST_PORT
        while port in used:
            port += 1
        return port

    def create(self, name, port, ini, start_after_install=True):
        if any(i.port == port for i in self.instances.values()):
            raise ValueError("port_in_use")
        iid = new_id(name, self.instances)
        root = self.data_dir / "servers" / iid
        root.mkdir(parents=True)
        write_json(root / "instance.json", {"name": name, "port": port, "initial_ini": ini,
                                            "created": datetime.now().isoformat(timespec="seconds"),
                                            "schedules": [{"id": secrets.token_hex(4), "cron": "0 5 * * *",
                                                           "action": "update", "enabled": True}]})
        inst = Instance(root, self.firewall, self.steamcmd_dir)
        self.instances[iid] = inst
        inst.install(then_start=start_after_install)
        return inst

    def delete(self, iid):
        inst = self.get(iid)
        if inst.task:
            raise InstanceBusy(inst.task)
        inst.stop()
        del self.instances[iid]
        inst.remove_files()

    # ---------- background ----------
    def start_background(self):
        for inst in self.instances.values():
            if inst.config.get("auto_start") and inst.installed and not inst.running():
                try:
                    inst.start()
                except Exception as exc:
                    inst.message = str(exc)
        threading.Thread(target=self._loop, daemon=True, name="supervisor").start()
        threading.Thread(target=self._scheduler, daemon=True, name="scheduler").start()

    def _loop(self):
        last_expire = 0
        while not self._stop.is_set():
            for inst in list(self.instances.values()):
                try:
                    lines = inst.tick()
                    if lines:
                        self._ingest(inst, lines)
                except Exception:
                    log.exception("%s: supervision failed", inst.id)
            if time.time() - last_expire > 60:
                last_expire = time.time()
                try:
                    self.firewall.expire()
                except Exception:
                    log.exception("expiring blocks failed")
            self._stop.wait(2)

    def _ingest(self, inst, lines):
        for join in inst.history.ingest(lines):
            self._enforce_whitelist(inst, join)

    def _enforce_whitelist(self, inst, join):
        """A player who is not on the whitelist joined: block their IP on this server's port, like a kick."""
        wl = inst.history.whitelist()
        owner = inst.files.read_ini()[0].get("OwnerId", "")
        if not wl["enabled"] or join["id"] == owner or join["id"] in wl["ids"]:
            return
        blocked = False
        if self.firewall.available and join["ip"] and join["ip"] not in inst.history.online_ips(set(wl["ids"]) | {owner}):
            try:
                self.firewall.block(join["ip"], inst.port, int(inst.config["block_minutes"]))
                blocked = True
            except Exception:
                log.exception("whitelist: blocking %s failed", join["ip"])
        log.warning("%s: whitelist refused %s (%s) ip %s %s", inst.id, join["name"], join["id"], join["ip"],
                    "blocked" if blocked else "NOT blocked")
        inst.history.add_refused(join, blocked)

    def _scheduler(self):
        last_minute = None
        while not self._stop.is_set():
            now = datetime.now().replace(second=0, microsecond=0)
            if now != last_minute:
                last_minute = now
                for inst in list(self.instances.values()):
                    for task in inst.config.get("schedules", []):
                        if not task.get("enabled"):
                            continue
                        try:
                            due = schedule.Cron(task["cron"]).matches(now)
                        except schedule.CronError:
                            continue
                        if due:
                            threading.Thread(target=self.run_task, args=(inst, task), daemon=True).start()
            self._stop.wait(15)

    def run_task(self, inst, task):
        action = task["action"]
        log.info("%s: scheduled %s (%s)", inst.id, action, task["cron"])
        try:
            if action == "backup":
                if inst.running() or inst.installed:
                    inst.with_stopped(lambda: None, backup=True)
            elif action == "restart":
                if inst.running():
                    inst.stop()
                    inst.start()
            elif action == "update":
                inst.update()
            elif action == "start":
                inst.start()
            elif action == "stop":
                inst.stop()
            task["last"], task["last_result"] = datetime.now().isoformat(timespec="seconds"), "ok"
        except Exception as exc:
            log.exception("%s: scheduled %s failed", inst.id, action)
            task["last"], task["last_result"] = datetime.now().isoformat(timespec="seconds"), str(exc)[:200]
        inst.save_config()

    def shutdown(self):
        """Stop every server cleanly (they save the world on a clean stop), in parallel."""
        self._stop.set()
        threads = [threading.Thread(target=i.stop) for i in self.instances.values() if i.running()]
        for t in threads:
            t.start()
        for t in threads:
            t.join(180)
