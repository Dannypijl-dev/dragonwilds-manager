"""Follow the server's log file (Saved/Logs/RSDragonwilds.log) as it grows.

The server writes the same log file on Windows and Linux and rotates it at every start
(the old one becomes RSDragonwilds-backup-<date>.log), so reading the file works the same
everywhere, also for a server process the manager adopted after its own restart.
"""
import collections
import re
import threading
from pathlib import Path

from . import history, players

READY_MARKER = "START SESSION - Success"  # world loaded and session advertised
JOIN_CODE = re.compile(r'Setting \["JoinCode"\] written with key\[\w+\] value\[([^\]]+)\]')
NOISE = ("LogStringTable", "LogGameFeatures", "HeartbeatSession")
# The server logs the world password: plain, as Password[...], and base64 in login URLs (?p=...)
SECRETS = [(re.compile(r"(WorldPassword: ).*"), r"\1••••"), (re.compile(r"(Password\[)[^\]]*"), r"\1••••"),
           (re.compile(r"([?&]p=)[^?&\s]*"), r"\1••••")]
PLAYER_LINES = re.compile(players.GREP)
INTERESTING = re.compile(f"{players.GREP}|{history.GREP}")
MAX_READ = 8 * 1024 * 1024  # catching up on a big file: only the tail matters for the console


def mask(line):
    for rx, repl in SECRETS:
        line = rx.sub(repl, line)
    return line


class LogWatcher:
    def __init__(self, path, buffer_lines=1500):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.lines = collections.deque(maxlen=buffer_lines)  # (seq, masked line)
        self.seq = 0
        self._identity = None
        self._offset = 0
        self._partial = b""
        self.reset_run()

    def reset_run(self):
        self.ready = False
        self.join_code = None
        self.player_lines = []

    def _file_identity(self):
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None, 0
        return (getattr(st, "st_ino", 0) or 0, int(st.st_ctime)), st.st_size

    def poll(self):
        """Read what was added since the last call. Returns the new raw lines (oldest first)."""
        identity, size = self._file_identity()
        if identity is None:
            return []
        new_run = identity != self._identity or size < self._offset
        with self.lock:
            if new_run:
                self._identity, self._offset, self._partial = identity, 0, b""
                self.reset_run()
                self._push("── log restarted ──")
            if size == self._offset:
                return []
            start = self._offset
            if new_run and size > MAX_READ:
                # don't load hundreds of MB; markers we need are re-derived from the part we read
                start = size - MAX_READ
            try:
                with self.path.open("rb") as f:
                    f.seek(start)
                    data = f.read(size - start)
            except OSError:
                return []
            self._offset = start + len(data)
            skipped = []
            if new_run and start > 0:
                data = data.split(b"\n", 1)[-1]  # drop the cut first line
                skipped = self._scan_head(start)
            data = self._partial + data
            parts = data.split(b"\n")
            self._partial = parts.pop()
            out = skipped
            for raw in parts:
                line = raw.decode("utf-8", "replace").lstrip("﻿").rstrip("\r")
                if not line:
                    continue
                out.append(line)
                if READY_MARKER in line:
                    self.ready = True
                elif m := JOIN_CODE.search(line):
                    self.join_code = m.group(1)
                if PLAYER_LINES.search(line):
                    self.player_lines.append(line)
                    del self.player_lines[:-2000]
                if not any(n in line for n in NOISE):
                    self._push(mask(line))
            return out

    def _scan_head(self, end):
        """The start of a big file that the console skips: still pick up the ready marker, the
        join code and player/history lines from it. Returns those lines."""
        keep = []
        try:
            with self.path.open("rb") as f:
                read = 0
                for raw in f:
                    read += len(raw)
                    if read > end:
                        break
                    line = raw.decode("utf-8", "replace").lstrip("\ufeff").rstrip("\r\n")
                    if READY_MARKER in line:
                        self.ready = True
                    elif m := JOIN_CODE.search(line):
                        self.join_code = m.group(1)
                    elif INTERESTING.search(line):
                        keep.append(line)
                        if PLAYER_LINES.search(line):
                            self.player_lines.append(line)
        except OSError:
            pass
        del self.player_lines[:-2000]
        return keep

    def _push(self, line):
        self.seq += 1
        self.lines.append((self.seq, line))

    def tail(self, after=0, limit=400):
        with self.lock:
            rows = [(s, l) for s, l in self.lines if s > after]
        return rows[-limit:], self.seq

    def players(self):
        with self.lock:
            lines = list(self.player_lines)
        return players.parse(lines)
