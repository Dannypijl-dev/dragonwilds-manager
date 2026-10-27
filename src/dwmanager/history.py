"""Play time per player, refused joins and the whitelist, kept in a JSON file.

A background thread feeds new server log lines to `History.ingest`:
  [2026.10.07-22.11.39:612][620]LogDomMatcherSession: Player ADDED to session [<id>]-[<name>]
  [2026.10.07-22.49.41:267][701]LogDomMatcherSession: Player Removed from session [<id>]-[<name>]
plus the connection (IP), login (platform), start, exit and EOS heartbeat (every 30 s) lines.
A session still open when the server stops ends at the exit line; after a crash it ends at
the last line seen before the next start. Log timestamps are UTC.
"""
import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .players import _ACCEPT, _LOGIN, _PLATFORM
from .util import replace_file

log = logging.getLogger("dwmanager.history")
GREP = ("NotifyAcceptedConnection|Login request|Player ADDED to session|Player Removed from session"
        "|HeartbeatSession|Starting Game\\.|LogExit: Exiting")
_TS = re.compile(r"^\[(\d{4})\.(\d\d)\.(\d\d)-(\d\d)\.(\d\d)\.(\d\d):(\d{3})\]\[\s*\d+\]")
_ADDED = re.compile(r"Player ADDED to session \[([^\]]+)\]-\[(.*)\]\s*$")
_REMOVED = re.compile(r"Player Removed from session \[([^\]]+)\]-\[(.*)\]\s*$")
SESSIONS_KEPT = 300
REFUSED_KEPT = 50
LIVE = timedelta(minutes=10)  # joins older than this are history, not something to act on


def _ts(line):
    m = _TS.match(line)
    if not m:
        return None  # the server also logs some lines without timestamp; those are duplicates
    y, mo, d, h, mi, s, ms = map(int, m.groups())
    return datetime(y, mo, d, h, mi, s, ms * 1000, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class History:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.pending = {"ip": None, "id": None, "platform": None}  # connection/login lines before a join
        try:
            self.data = json.loads(self.path.read_text())
        except FileNotFoundError:
            self.data = {}
        except ValueError:
            log.exception("history file unreadable, starting over: %s", self.path)
            self.data = {}
        for key, default in (("last_ts", None), ("boundary", []), ("players", {}), ("open", {}),
                             ("sessions", []), ("refused", []), ("whitelist", {"enabled": False, "ids": []})):
            self.data.setdefault(key, default)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        replace_file(self.path, json.dumps(self.data, indent=1))

    # ---------- reading the log ----------
    def ingest(self, lines, now=None):
        """Process log lines (oldest first; already seen ones are skipped). Returns recent joins."""
        now = now or datetime.now(timezone.utc)
        joins = []
        with self.lock:
            d = self.data
            last = parse_iso(d["last_ts"]) if d["last_ts"] else None
            boundary = set(d["boundary"])
            changed = False
            for raw in lines:
                line = raw.strip()
                ts = _ts(line)
                if ts is None or (last and (ts < last or (ts == last and line in boundary))):
                    continue
                if ts != last:
                    boundary = set()
                boundary.add(line)
                body = _TS.sub("", line)
                if "Starting Game." in body:
                    self._close_all(last or ts)  # crash: nothing logged at the end, so use the last line
                elif "LogExit: Exiting" in body:
                    self._close_all(ts)
                elif m := _ACCEPT.search(body):
                    self.pending = {"ip": m.group(1), "id": None, "platform": None}
                elif m := _LOGIN.search(body):
                    pf = _PLATFORM.search(m.group(1))
                    self.pending.update(id=m.group(2), platform=pf.group(1) if pf else None)
                elif m := _ADDED.search(body):
                    pid, name = m.group(1), m.group(2)
                    info = self.pending if self.pending["id"] == pid else {"ip": None, "platform": None}
                    self._close(pid, ts)  # a rejoin without a logout line
                    d["open"][pid] = {"name": name, "start": _iso(ts), "ip": info["ip"]}
                    p = d["players"].setdefault(pid, {"name": name, "platform": None, "seconds": 0,
                                                      "sessions": 0, "first": _iso(ts), "last": _iso(ts)})
                    p.update(name=name, last=_iso(ts), sessions=p["sessions"] + 1)
                    if info["platform"]:
                        p["platform"] = info["platform"]
                    self.pending = {"ip": None, "id": None, "platform": None}
                    if now - ts < LIVE:
                        joins.append({"id": pid, "name": name, "ip": info["ip"], "at": ts})
                elif m := _REMOVED.search(body):
                    self._close(m.group(1), ts)
                last = ts
                changed = True
            if changed:
                d["last_ts"], d["boundary"] = _iso(last), sorted(boundary)
                self.save()
        return joins

    def _close(self, pid, end):
        s = self.data["open"].pop(pid, None)
        if not s:
            return
        start = parse_iso(s["start"])
        seconds = max(0, int((end - start).total_seconds()))
        p = self.data["players"][pid]
        p["seconds"] += seconds
        p["last"] = _iso(max(end, start))
        self.data["sessions"].append({"id": pid, "name": s["name"], "start": s["start"], "seconds": seconds})
        del self.data["sessions"][:-SESSIONS_KEPT]

    def _close_all(self, end):
        for pid in list(self.data["open"]):
            self._close(pid, end)

    # ---------- whitelist ----------
    def whitelist(self):
        with self.lock:
            wl = self.data["whitelist"]
            return {"enabled": wl["enabled"], "ids": list(wl["ids"])}

    def set_whitelist_enabled(self, enabled):
        with self.lock:
            self.data["whitelist"]["enabled"] = enabled
            self.save()

    def set_allowed(self, pid, allowed):
        with self.lock:
            ids = self.data["whitelist"]["ids"]
            if allowed and pid not in ids:
                ids.append(pid)
            elif not allowed and pid in ids:
                ids.remove(pid)
            self.save()

    def add_refused(self, join, blocked):
        with self.lock:
            self.data["refused"].append({"id": join["id"], "name": join["name"], "ip": join["ip"],
                                         "at": _iso(join["at"]), "blocked": blocked})
            del self.data["refused"][:-REFUSED_KEPT]
            self.save()

    def online_ips(self, ids):
        """IPs of players in `ids` that are in the world right now."""
        with self.lock:
            return {s["ip"] for pid, s in self.data["open"].items() if pid in ids and s.get("ip")}

    # ---------- for the UI ----------
    def snapshot(self):
        """Players with total play time (open sessions counted up to the last log line), sessions, refused."""
        with self.lock:
            d = json.loads(json.dumps(self.data))
        last = parse_iso(d["last_ts"]) if d["last_ts"] else None
        for pid, s in d["open"].items():
            p = d["players"][pid]
            running = max(0, int((last - parse_iso(s["start"])).total_seconds())) if last else 0
            p["seconds"] += running
            p["online"] = True
            d["sessions"].append({"id": pid, "name": s["name"], "start": s["start"], "seconds": running, "open": True})
        return d
