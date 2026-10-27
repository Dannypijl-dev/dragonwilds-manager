"""Pak mods: .pak files (usually with a matching .utoc and .ucas) in Content/Paks/~mods.

Only pak mods load on a dedicated server. UE4SS (Lua) mods hook into the Windows game client
and are installed on the player's PC instead; uploads that look like one are refused with
that explanation. A disabled mod is moved to the instance's mods-disabled folder.
"""
import shutil
import zipfile
from pathlib import Path

from .util import safe_filename

PAK_EXT = {".pak", ".utoc", ".ucas", ".sig"}
MAX_MOD_BYTES = 4 * 1024**3


class ModError(ValueError):
    pass


def list_mods(enabled_dir, disabled_dir):
    mods = {}
    for folder, enabled in ((Path(enabled_dir), True), (Path(disabled_dir), False)):
        if not folder.exists():
            continue
        for f in folder.iterdir():
            if f.suffix.lower() in PAK_EXT and f.is_file():
                m = mods.setdefault(f.stem, {"name": f.stem, "enabled": enabled, "files": [], "size": 0})
                m["files"].append(f.suffix.lower())
                m["size"] += f.stat().st_size
    for m in mods.values():
        m["complete"] = ".pak" in m["files"] and (".utoc" in m["files"]) == (".ucas" in m["files"])
        m["files"].sort()
    return sorted(mods.values(), key=lambda m: m["name"].lower())


def install_upload(filename, path, enabled_dir):
    """Install an uploaded .pak/.utoc/.ucas file or a .zip containing them (saved at `path`).
    Returns the mod names."""
    enabled_dir = Path(enabled_dir)
    files = {}
    lower = filename.lower()
    if lower.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            names = [i.filename for i in z.infolist() if not i.is_dir()]
            low = [n.lower() for n in names]
            if any(n.endswith(".lua") for n in low) or any(n.endswith("enabled.txt") for n in low):
                raise ModError("ue4ss")
            if any(n.endswith(".sav") for n in low) and not any(Path(n).suffix in PAK_EXT for n in low):
                raise ModError("save")
            for info in z.infolist():
                if info.is_dir() or Path(info.filename.lower()).suffix not in PAK_EXT:
                    continue
                if info.file_size > MAX_MOD_BYTES:
                    raise ModError("too_large")
                files[safe_filename(info.filename)] = info
            if not files:
                raise ModError("empty")
            enabled_dir.mkdir(parents=True, exist_ok=True)
            for name, info in files.items():
                with z.open(info) as src, open(enabled_dir / name, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
    elif Path(lower).suffix in PAK_EXT:
        name = safe_filename(filename)
        enabled_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, enabled_dir / name)
        files[name] = None
    else:
        raise ModError("type")
    return sorted({Path(n).stem for n in files})


def set_enabled(name, enabled, enabled_dir, disabled_dir):
    src, dst = (Path(disabled_dir), Path(enabled_dir)) if enabled else (Path(enabled_dir), Path(disabled_dir))
    moved = 0
    for f in list(src.glob("*")) if src.exists() else []:
        if f.stem == name and f.suffix.lower() in PAK_EXT:
            dst.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(dst / f.name))
            moved += 1
    if not moved:
        raise ModError("unknown")


def delete(name, enabled_dir, disabled_dir):
    removed = 0
    for folder in (Path(enabled_dir), Path(disabled_dir)):
        for f in list(folder.glob("*")) if folder.exists() else []:
            if f.stem == name and f.suffix.lower() in PAK_EXT:
                f.unlink()
                removed += 1
    if not removed:
        raise ModError("unknown")
