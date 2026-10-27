"""World saves: list them, import a .sav, pick the active one, delete.

The server finds a world by the name stored *inside* the .sav (WorldSaveSettings.WorldName),
not by the file name. DefaultWorldName in the ini has to be that inner name, and an imported
save is stored as "<inner name>.sav" so both agree.
"""
import zipfile
from datetime import datetime
from pathlib import Path

from . import world_settings
from .util import safe_filename

MAX_SAVE_BYTES = 512 * 1024 * 1024


class WorldError(ValueError):
    pass


def inner_name(data_or_path):
    raw = Path(data_or_path).read_bytes() if isinstance(data_or_path, (str, Path)) else bytes(data_or_path)
    try:
        return world_settings._parse(raw)["world"]
    except Exception as exc:  # any parse error means "not a world save we understand"
        raise WorldError("not a Dragonwilds world save (.sav)") from exc


def list_worlds(saves_dir, active_name):
    rows = []
    for path in sorted(Path(saves_dir).glob("*.sav")):
        info = world_settings.read(path)
        rows.append({
            "file": path.name,
            "name": info["world"] if info else path.stem,
            "valid": info is not None,
            "active": bool(info) and info["world"] == active_name,
            "size": path.stat().st_size,
            "date": datetime.fromtimestamp(path.stat().st_mtime),
            "difficulty": info["difficulty"] if info else None,
            "friendly_fire": info["friendly_fire"] if info else None,
        })
    if active_name and not any(r["active"] for r in rows):
        rows.append({"file": None, "name": active_name, "valid": True, "active": True, "size": 0, "date": None,
                     "difficulty": None, "friendly_fire": None, "new": True})
    return rows


def extract_upload(filename, path):
    """An uploaded .sav, or a .zip with exactly one .sav in it (saved at `path`). Returns (inner name, bytes)."""
    if Path(path).stat().st_size > MAX_SAVE_BYTES * 2:
        raise WorldError("file too large")
    data = None
    if filename.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            saves = [i for i in z.infolist() if i.filename.lower().endswith(".sav") and not i.is_dir()]
            if len(saves) != 1:
                raise WorldError("the zip must contain exactly one .sav file")
            if saves[0].file_size > MAX_SAVE_BYTES:
                raise WorldError("save file too large")
            data = z.read(saves[0])
    elif filename.lower().endswith(".sav"):
        data = Path(path).read_bytes()
    else:
        raise WorldError("upload a .sav file or a .zip with one .sav in it")
    return inner_name(data), data


def import_save(saves_dir, data, overwrite=False):
    name = inner_name(data)
    fname = safe_filename(f"{name}.sav")
    if fname != f"{name}.sav":
        raise WorldError(f"world name '{name}' has characters that can't be used as a file name")
    target = Path(saves_dir) / fname
    if target.exists() and not overwrite:
        raise WorldError(f"a world called '{name}' already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    Path(saves_dir, fname + ".backup").unlink(missing_ok=True)  # the server's own previous copy
    return name


def save_file(saves_dir, file):
    p = Path(saves_dir) / Path(file).name
    if p.suffix != ".sav" or not p.is_file():
        raise FileNotFoundError(file)
    return p


def delete(saves_dir, file):
    p = save_file(saves_dir, file)
    p.unlink()
    p.with_name(p.name + ".backup").unlink(missing_ok=True)
