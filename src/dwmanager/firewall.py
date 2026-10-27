"""Temporarily block a player's IP on one server's game port: used for kick and the whitelist.

Dragonwilds has no admin command channel, so a "kick" drops the player's packets until they
time out, and keeps them out for the chosen time.

- Linux: an nftables table of its own (never touches ufw or other rules). Needs root; when
  the manager runs as a normal user it calls the root-owned helper `dwm-firewall` via sudo.
- Windows: Windows Firewall block rules ("Dragonwilds Manager block ...") through netsh.
- Expiry is kept in blocks.json and enforced by the manager (nftables also times out by itself).

Opening the game port in the host firewall: Windows Firewall (netsh) on Windows, ufw on Linux
when it is installed (through the same root helper). Other Linux firewalls are left to the admin.
"""
import ipaddress
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import platforms
from .util import read_json, write_json

log = logging.getLogger("dwmanager.firewall")
TABLE = "inet dragonwilds_manager"
LINUX_HELPER = "/usr/local/lib/dragonwilds-manager/dwm-firewall"
RULE_PREFIX = "Dragonwilds Manager block"
PORT_RULE_PREFIX = "Dragonwilds Manager port"
UFW = "/usr/sbin/ufw"
PORT_CACHE_SECONDS = 30


def valid_ip(ip):
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return not (addr.is_loopback or addr.is_unspecified)


def _run(cmd, timeout=30):
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if platforms.WINDOWS else {}
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kwargs)
    if p.returncode != 0:
        raise RuntimeError((p.stdout + p.stderr).strip() or f"{cmd[0]} failed")
    return p.stdout


class Firewall:
    def __init__(self, state_file):
        self.state_file = Path(state_file)
        self.lock = threading.Lock()
        self.mode = self._detect()
        self.port_mode = self._detect_port_mode()
        self._port_cache = {}  # port -> (checked_at, open)

    def _detect(self):
        forced = os.environ.get("DWM_FIREWALL", "").lower()
        if forced in ("none", "off", "0"):
            return None
        if platforms.WINDOWS:
            return "netsh" if shutil.which("netsh") else None
        if os.geteuid() == 0:
            return "nft" if shutil.which("nft") else None
        if Path(LINUX_HELPER).exists() and shutil.which("sudo"):
            return "helper"
        return None

    @property
    def available(self):
        return self.mode is not None

    def _detect_port_mode(self):
        """How the game port can be opened in the host firewall, or None when the admin must do it."""
        if self.mode == "netsh":
            return "netsh"
        if self.mode in ("nft", "helper") and (Path(UFW).exists() or shutil.which("ufw")):
            return "ufw"
        return None

    # ---------- game port in the host firewall ----------
    def port_is_open(self, port):
        """True/False, or None when the host firewall can't be queried."""
        if not self.port_mode:
            return None
        cached = self._port_cache.get(port)
        if cached and time.time() - cached[0] < PORT_CACHE_SECONDS:
            return cached[1]
        try:
            if self.port_mode == "ufw":
                out = (_run(["sudo", "-n", LINUX_HELPER, "port", "status", str(port)]) if self.mode == "helper"
                       else ufw_port("status", int(port)))
                result = out.strip().endswith("open")
            else:
                probe = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={PORT_RULE_PREFIX} UDP {int(port)}"],
                                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                result = probe.returncode == 0
        except Exception:
            log.exception("checking the host firewall for port %s failed", port)
            return None
        self._port_cache[port] = (time.time(), result)
        return result

    def set_port(self, port, open_):
        """Open or close the game port in the host firewall."""
        if not self.port_mode:
            raise RuntimeError("port_unavailable")
        port = int(port)
        if not 1 <= port <= 65535:
            raise ValueError("invalid port")
        action = "open" if open_ else "close"
        if self.port_mode == "ufw":
            if self.mode == "helper":
                _run(["sudo", "-n", LINUX_HELPER, "port", action, str(port)])
            else:
                ufw_port(action, port)
        else:
            name = f"{PORT_RULE_PREFIX} UDP {port}"
            subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if open_:
                _run(["netsh", "advfirewall", "firewall", "add", "rule", f"name={name}", "dir=in", "action=allow",
                      "protocol=UDP", f"localport={port}"])
        self._port_cache.pop(port, None)

    def ensure_port(self, port):
        """Best effort at server start: open the port if the host firewall is managed and it isn't open yet."""
        try:
            if self.port_mode and self.port_is_open(port) is False:
                self.set_port(port, True)
                log.info("opened UDP %s in the host firewall (%s)", port, self.port_mode)
        except Exception:
            log.exception("opening UDP %s in the host firewall failed", port)

    def _blocks(self):
        return read_json(self.state_file, [])

    def block(self, ip, port, minutes):
        if not self.available:
            raise RuntimeError("blocking players is not available on this host")
        if not valid_ip(ip) or not (1 <= int(minutes) <= 10080):
            raise ValueError("invalid ip or duration")
        with self.lock:
            self._apply("add", ip, port, minutes)
            blocks = [b for b in self._blocks() if not (b["ip"] == ip and b["port"] == port)]
            blocks.append({"ip": ip, "port": int(port), "until": time.time() + int(minutes) * 60})
            write_json(self.state_file, blocks)

    def unblock(self, ip, port):
        with self.lock:
            if self.available:
                self._apply("del", ip, port)
            write_json(self.state_file, [b for b in self._blocks() if not (b["ip"] == ip and b["port"] == port)])

    def blocked(self, port):
        now = time.time()
        return [{"ip": b["ip"], "seconds": int(b["until"] - now)} for b in self._blocks()
                if b["port"] == port and b["until"] > now]

    def expire(self):
        """Lift blocks whose time is up (called every minute by the manager)."""
        now = time.time()
        with self.lock:
            blocks = self._blocks()
            keep = []
            for b in blocks:
                if b["until"] > now:
                    keep.append(b)
                    continue
                try:
                    if self.available:
                        self._apply("del", b["ip"], b["port"])
                except Exception:
                    log.exception("lifting block %s failed", b)
            if len(keep) != len(blocks):
                write_json(self.state_file, keep)

    # ---------- backends ----------
    def _apply(self, action, ip, port, minutes=None):
        port = int(port)
        if self.mode == "helper":
            _run(["sudo", "-n", LINUX_HELPER, action, ip, str(port)] + ([str(minutes)] if minutes else []))
        elif self.mode == "nft":
            nft_apply(action, ip, port, minutes)
        elif self.mode == "netsh":
            name = f"{RULE_PREFIX} {ip} UDP {port}"
            subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if action == "add":
                _run(["netsh", "advfirewall", "firewall", "add", "rule", f"name={name}", "dir=in", "action=block",
                      "protocol=UDP", f"localport={port}", f"remoteip={ip}"])


def nft_apply(action, ip, port, minutes=None):
    """Shared with the Linux helper script (`python -m dwmanager firewall ...` runs this as root)."""
    if not valid_ip(ip) or not (1 <= port <= 65535):
        raise ValueError("invalid ip or port")
    probe = subprocess.run(["nft", "list", "table", *TABLE.split()], capture_output=True)
    if probe.returncode != 0:
        _nft_script(f"""table {TABLE} {{
  set blocked {{ type ipv4_addr . inet_service; flags timeout; }}
  chain input {{
    type filter hook input priority -10; policy accept;
    ip saddr . udp dport @blocked drop
  }}
}}""")
    subprocess.run(["nft", "delete", "element", *TABLE.split(), "blocked", f"{{ {ip} . {port} }}"], capture_output=True)
    if action == "add":
        _run(["nft", "add", "element", *TABLE.split(), "blocked", f"{{ {ip} . {port} timeout {int(minutes)}m }}"])


def _nft_script(text):
    p = subprocess.run(["nft", "-f", "-"], input=text, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "nft failed")


def ufw_port(action, port):
    """Root side (also used by the Linux helper): open/close/status of a UDP port in ufw."""
    if not 1 <= int(port) <= 65535:
        raise ValueError("invalid port")
    ufw = UFW if Path(UFW).exists() else shutil.which("ufw")
    if not ufw:
        raise RuntimeError("ufw is not installed")
    rule = f"{int(port)}/udp"
    if action == "status":
        out = subprocess.run([ufw, "status"], capture_output=True, text=True).stdout
        is_open = any(line.split()[:2] == [rule, "ALLOW"] for line in out.splitlines() if line.strip())
        return f"{rule} {'open' if is_open else 'closed'}"
    if action == "open":
        _run([ufw, "allow", rule, "comment", "Dragonwilds Manager"])
    elif action == "close":
        _run([ufw, "--force", "delete", "allow", rule])
    else:
        raise ValueError("action must be open, close or status")
    return ""
