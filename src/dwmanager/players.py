"""Work out who is online from the server log.

The server has no query API, so this follows log lines since the last start:
  NotifyAcceptedConnection: ... RemoteAddr: <ip>:<port>, ...
  Login request: ?p=<pw>?pf=<platform>?...?Name=<name> userId: RedpointEOS:<id> platform: RedpointEOS
  Join succeeded: <player name>
  Player Removed from session [<id>]-[<player name>]
"""
import re

GREP = "NotifyAcceptedConnection|Login request|Join succeeded|Player Removed from session"
_ACCEPT = re.compile(r"NotifyAcceptedConnection: .*RemoteAddr: (\d+\.\d+\.\d+\.\d+):\d+")
_LOGIN = re.compile(r"Login request: (\S*) userId: (?:\w+:)?(\S+)")
_PLATFORM = re.compile(r"[?&]pf=([^?&]+)")
_JOIN = re.compile(r"Join succeeded: (.+)$")
_LOGOUT = re.compile(r"Player Removed from session \[([^\]]+)\]-\[(.*)\]\s*$")
_PREFIX = re.compile(r"^\[[^\]]+\]\[\s*\d+\]")  # "[2026.10.07-14.37.27:116][  0]"


def parse(lines):
    online, events, pending, previous, ip = {}, [], None, None, None
    for raw in lines:
        line = _PREFIX.sub("", raw).strip()
        if line == previous:  # the server logs some lines twice in a row (with and without timestamp)
            continue
        previous = line
        if m := _ACCEPT.search(line):
            ip = m.group(1)
        elif m := _LOGIN.search(line):
            pf = _PLATFORM.search(m.group(1))
            pending = {"id": m.group(2), "platform": pf.group(1) if pf else "?", "ip": ip}
            ip = None
        elif m := _JOIN.search(line):
            name = m.group(1).strip()
            online[name] = {"name": name, **(pending or {"id": "?", "platform": "?", "ip": None})}
            pending = None
            events.append(("join", name))
        elif m := _LOGOUT.search(line):
            pid, who = m.groups()
            for name, p in list(online.items()):
                if p["id"] == pid or name == who:
                    del online[name]
                    who = name
                    break
            events.append(("leave", who))
    return list(online.values()), events[-15:]
