"""Install SteamCMD and use it to install or update the Dragonwilds server."""
import io
import logging
import os
import re
import shutil
import subprocess
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

from . import platforms

log = logging.getLogger("dwmanager.steamcmd")
PROGRESS = re.compile(r"Update state \(0x[0-9a-f]+\) (\w+), progress: ([\d.]+)")


class SteamCmdError(RuntimeError):
    pass


def ensure(steamcmd_dir):
    """Download SteamCMD into `steamcmd_dir` if it isn't there yet. Returns the executable."""
    steamcmd_dir = Path(steamcmd_dir)
    exe = platforms.steamcmd_executable(steamcmd_dir)
    if exe.exists():
        return exe
    steamcmd_dir.mkdir(parents=True, exist_ok=True)
    log.info("downloading SteamCMD from %s", platforms.STEAMCMD_URL)
    with urllib.request.urlopen(platforms.STEAMCMD_URL, timeout=120) as resp:
        data = resp.read()
    if platforms.WINDOWS:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extractall(steamcmd_dir)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            if hasattr(tarfile, "data_filter"):
                t.extractall(steamcmd_dir, filter="data")
            else:
                t.extractall(steamcmd_dir)
        exe.chmod(0o755)
        for f in steamcmd_dir.rglob("steamcmd"):
            f.chmod(0o755)
    if not exe.exists():
        raise SteamCmdError(f"SteamCMD not found after download in {steamcmd_dir}")
    return exe


def install_server(steamcmd_dir, server_dir, on_progress=None, validate=True, attempts=3):
    """Install or update the server into `server_dir`. Calls on_progress(stage, percent) while downloading.

    SteamCMD regularly fails the first time with "Missing configuration" or exits early after
    updating itself, so this retries.
    """
    exe = ensure(steamcmd_dir)
    server_dir = Path(server_dir)
    server_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(exe), "+@sSteamCmdForcePlatformType", platforms.STEAM_PLATFORM,
           "+force_install_dir", str(server_dir.resolve()), "+login", "anonymous",
           "+app_update", platforms.APP_ID] + (["validate"] if validate else []) + ["+quit"]
    env = os.environ.copy()
    if not platforms.WINDOWS:
        # SteamCMD keeps its own config under $HOME; give it one that is always writable
        home = Path(steamcmd_dir) / "home"
        home.mkdir(parents=True, exist_ok=True)
        env["HOME"] = str(home)
    last = ""
    for attempt in range(1, attempts + 1):
        ok, last = _run(cmd, on_progress, env)
        if ok:
            return True
        log.warning("SteamCMD attempt %d failed: %s", attempt, last)
        if on_progress:
            on_progress("retry", None)
        time.sleep(10 if attempt < attempts else 0)
    raise SteamCmdError(last or "SteamCMD failed")


def _run(cmd, on_progress, env=None):
    kwargs = {"env": env}
    if platforms.WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kwargs)
    tail, ok = [], False
    buf = b""
    # SteamCMD writes progress lines ending in \r or \n
    while chunk := proc.stdout.read1(4096):
        buf += chunk
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for raw in parts:
            line = re.sub(r"\x1b\[[0-9;]*m", "", raw.decode("utf-8", "replace")).strip()
            if not line:
                continue
            tail = (tail + [line])[-20:]
            if m := PROGRESS.search(line):
                if on_progress:
                    on_progress(m.group(1), float(m.group(2)))
            if f"Success! App '{platforms.APP_ID}'" in line:
                ok = True
    proc.wait()
    errors = [l for l in tail if "ERROR" in l or "Failed" in l]
    return ok, (errors[-1] if errors else (tail[-1] if tail else f"exit code {proc.returncode}"))


def build_id(server_dir):
    try:
        text = (Path(server_dir) / f"steamapps/appmanifest_{platforms.APP_ID}.acf").read_text()
    except OSError:
        return None
    m = re.search(r'"buildid"\s+"(\d+)"', text)
    return m.group(1) if m else None


def remove(server_dir):
    shutil.rmtree(server_dir, ignore_errors=True)
