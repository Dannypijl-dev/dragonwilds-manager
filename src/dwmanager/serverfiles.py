"""The server's own files: DedicatedServer.ini, known players / bans, backups."""
import os
import re
import shutil
import tarfile
import zipfile
from datetime import datetime
from pathlib import Path

from . import platforms
from .util import replace_file

SECTION = "[/Script/Dominion.DedicatedServerSettings]"
POLICIES = ["Crossplay", "PC", "PlayStation", "Xbox", "Nintendo"]
# Folders under Saved/ that a backup leaves out: logs and crash dumps are large and not needed to restore
BACKUP_SKIP = {"Logs", "Crashes"}
BACKUP_GLOB = "backup-*.zip"


class ServerFiles:
    def __init__(self, server_dir, backup_dir, keep=14):
        self.server_dir = Path(server_dir)
        self.saved_dir = self.server_dir / "RSDragonwilds/Saved"
        self.backup_dir = Path(backup_dir)
        self.keep = keep

    @property
    def ini(self):
        return platforms.ini_path(self.saved_dir)

    @property
    def saves_dir(self):
        return self.saved_dir / "SaveGames"

    @property
    def log_file(self):
        return self.saved_dir / "Logs/RSDragonwilds.log"

    @property
    def mods_dir(self):
        return self.server_dir / "RSDragonwilds/Content/Paks/~mods"

    # ---------- DedicatedServer.ini ----------
    def read_ini(self):
        try:
            lines = self.ini.read_text(encoding="utf-8-sig").splitlines()
        except FileNotFoundError:
            lines = []
        values = {}
        for line in lines:
            if "=" in line and not line.lstrip().startswith((";", "[")):
                key, value = line.split("=", 1)
                if key.strip() != "KnownPlayerList":
                    values[key.strip()] = value
        return values, lines

    def write_ini(self, updates):
        """Change keys in the settings section and keep everything else.

        Only while the server is stopped: it rewrites the file from memory when it shuts down
        and drops keys it doesn't know.
        """
        _, lines = self.read_ini()
        out, done, in_section = [], set(), False

        def add_missing():
            trailing = []
            while out and not out[-1].strip():
                trailing.append(out.pop())
            out.extend(f"{k}={v}" for k, v in updates.items() if k not in done)
            out.extend(trailing)
            done.update(updates)

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("["):
                if in_section:
                    add_missing()
                in_section = stripped == SECTION
            elif in_section and "=" in line and not stripped.startswith(";"):
                key = line.split("=", 1)[0].strip()
                if key in updates:
                    line = f"{key}={updates[key]}"
                    done.add(key)
            out.append(line)
        if in_section:
            add_missing()
        if SECTION not in (l.strip() for l in lines):
            if not lines:
                out.append(";METADATA=(Diff=true, UseCommands=true)")
            out.append(SECTION)
            add_missing()
        self.ini.parent.mkdir(parents=True, exist_ok=True)
        replace_file(self.ini, "\n".join(out) + "\n")

    # ---------- known players / bans ----------
    # KnownPlayerList=(UserId=<id>,UserName="<name>",Privileges=(PrivilegeMask=14),bIsBanned=False)
    _KNOWN = re.compile(r'^KnownPlayerList=\(UserId=([^,]+),UserName="(.*)",Privileges=\(PrivilegeMask=(\d+)\),bIsBanned=(True|False)\)$')

    def known_players(self):
        players = []
        for line in self.read_ini()[1]:
            if m := self._KNOWN.match(line.strip()):
                players.append({"id": m.group(1), "name": m.group(2), "privileges": int(m.group(3)),
                                "banned": m.group(4) == "True"})
        return players

    def set_banned(self, user_id, banned):
        """Flip bIsBanned for one known player. Server must be stopped (see write_ini)."""
        _, lines = self.read_ini()
        changed = False
        for i, line in enumerate(lines):
            m = self._KNOWN.match(line.strip())
            if m and m.group(1) == user_id:
                lines[i] = line.replace(f"bIsBanned={m.group(4)})", f"bIsBanned={'True' if banned else 'False'})")
                changed = True
        if not changed:
            raise ValueError("unknown player")
        replace_file(self.ini, "\n".join(lines) + "\n")

    # ---------- backups ----------
    def backups(self):
        files = sorted(self.backup_dir.glob(BACKUP_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
        return [{"name": f.name, "size": f.stat().st_size, "date": datetime.fromtimestamp(f.stat().st_mtime)}
                for f in files]

    def backup_path(self, name):
        p = self.backup_dir / Path(name).name
        if not p.name.startswith("backup-") or p.suffix != ".zip" or not p.is_file():
            raise FileNotFoundError(name)
        return p

    def make_backup(self, label="manual"):
        """Zip the Saved/ folder (worlds, config) without logs and crash dumps. Server should be stopped."""
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        label = re.sub(r"[^a-z0-9-]", "", label.lower())[:20] or "manual"
        target = self.backup_dir / f"backup-{datetime.now():%Y-%m-%d_%H%M%S}-{label}.zip"
        tmp = target.with_suffix(".partial")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            if self.saved_dir.exists():
                for path in sorted(self.saved_dir.rglob("*")):
                    rel = path.relative_to(self.saved_dir)
                    if rel.parts[0] in BACKUP_SKIP or not path.is_file():
                        continue
                    z.write(path, Path("Saved") / rel)
        if os.name != "nt":
            os.chmod(tmp, 0o600)  # contains the world password
        os.replace(tmp, target)
        removed = []
        for old in [b["name"] for b in self.backups()][self.keep:]:
            (self.backup_dir / old).unlink()
            removed.append(old)
        return target, removed

    def restore_backup(self, archive):
        """Replace Saved/ (except logs) with the contents of a backup. Server must be stopped.

        Takes this manager's .zip backups and the .tar.gz backups of the older Dragonwilds Portal.
        """
        archive = Path(archive)
        staging = self.saved_dir.with_name("Saved.restore-tmp")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            if archive.suffix == ".zip":
                with zipfile.ZipFile(archive) as z:
                    for member in z.infolist():
                        _extract_member(member.filename, lambda: z.read(member), staging, member.is_dir())
            else:
                with tarfile.open(archive, "r:*") as t:
                    for member in t.getmembers():
                        if member.isfile():
                            _extract_member(member.name, lambda: t.extractfile(member).read(), staging, False)
            if not (staging / "SaveGames").exists() and not (staging / "Config").exists():
                raise ValueError("this archive has no Saved/SaveGames or Saved/Config folder")
            for name in [p.name for p in staging.iterdir()]:
                if name in BACKUP_SKIP:
                    continue
                dest = self.saved_dir / name
                if dest.exists():
                    shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
                shutil.move(str(staging / name), str(dest))
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        _match_owner(self.saved_dir)


def _extract_member(name, read, dest_root, is_dir):
    """Write one archive member below dest_root. Accepts 'Saved/...' paths and refuses anything outside."""
    parts = [p for p in re.split(r"[\\/]", name) if p not in ("", ".")]
    if parts and parts[0] == "Saved":
        parts = parts[1:]
    if not parts or ".." in parts or is_dir:
        return
    target = dest_root.joinpath(*parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(read())


def _match_owner(root):
    """On Linux the manager may run as root (Docker): keep files owned like their parent folder."""
    if os.name == "nt" or os.getuid() != 0:
        return
    st = root.parent.stat()
    for path in [root, *root.rglob("*")]:
        try:
            os.chown(path, st.st_uid, st.st_gid)
        except OSError:
            pass
