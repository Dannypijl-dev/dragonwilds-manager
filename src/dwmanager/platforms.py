"""Everything that differs between Windows and Linux."""
import os
import sys
from pathlib import Path

WINDOWS = os.name == "nt"
APP_ID = "4019830"  # RuneScape: Dragonwilds Dedicated Server (Steam, anonymous login)
STEAM_PLATFORM = "windows" if WINDOWS else "linux"
STEAMCMD_URL = ("https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip" if WINDOWS
                else "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz")
INI_PLATFORM_DIRS = ("WindowsServer", "Windows") if WINDOWS else ("LinuxServer", "Linux")
IN_DOCKER = Path("/.dockerenv").exists() or os.environ.get("DWM_IN_DOCKER") == "1"


def default_data_dir():
    """Where servers, backups and the manager's own settings live, unless DWM_DATA_DIR says otherwise."""
    if env := os.environ.get("DWM_DATA_DIR"):
        return Path(env)
    if IN_DOCKER:
        return Path("/data")
    if WINDOWS:
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "DragonwildsManager"
    if os.geteuid() == 0:
        return Path("/var/lib/dragonwilds-manager")
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "dragonwilds-manager"


def steamcmd_executable(steamcmd_dir):
    return Path(steamcmd_dir) / ("steamcmd.exe" if WINDOWS else "steamcmd.sh")


def server_executable(server_dir):
    """The real server binary (not the wrapper script), so we can watch and signal the process itself."""
    server_dir = Path(server_dir)
    if WINDOWS:
        candidates = sorted((server_dir / "RSDragonwilds/Binaries/Win64").glob("RSDragonwildsServer*.exe"))
        candidates += [server_dir / "RSDragonwildsServer.exe"]
    else:
        candidates = [server_dir / "RSDragonwilds/Binaries/Linux/RSDragonwildsServer-Linux-Shipping"]
    for c in candidates:
        if c.is_file():
            return c
    return None


def server_command(server_dir, port, extra=()):
    exe = server_executable(server_dir)
    if exe is None:
        return None
    return [str(exe), "RSDragonwilds", "-log", f"-Port={port}", *extra]


def ini_path(saved_dir):
    """DedicatedServer.ini: the server creates it on first start under Config/<platform>Server."""
    config = Path(saved_dir) / "Config"
    for name in INI_PLATFORM_DIRS:
        p = config / name / "DedicatedServer.ini"
        if p.exists():
            return p
    return config / INI_PLATFORM_DIRS[0] / "DedicatedServer.ini"


def frozen():
    return getattr(sys, "frozen", False)


def self_command(*args):
    """How to run this program again (for helper subprocesses), frozen exe or `python -m`."""
    if frozen():
        return [sys.executable, *args]
    return [sys.executable, "-m", "dwmanager", *args]
