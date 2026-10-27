"""Small helpers shared by the modules."""
import json
import os
import re
from pathlib import Path

SAFE_NAME = re.compile(r"[^A-Za-z0-9 ._'()\-]")


def replace_file(path, data, mode=0o600):
    """Atomically replace `path`. On Linux the original owner is kept (the server must still read it)."""
    path = Path(path)
    tmp = path.with_name(path.name + ".dwm-tmp")
    if isinstance(data, (bytes, bytearray)):
        tmp.write_bytes(data)
    else:
        tmp.write_text(data, encoding="utf-8")
    if os.name != "nt":
        os.chmod(tmp, mode)
        try:
            st = path.stat()
            if (st.st_uid, st.st_gid) != (os.getuid(), os.getgid()):
                os.chown(tmp, st.st_uid, st.st_gid)
        except (FileNotFoundError, PermissionError):
            pass
    os.replace(tmp, path)


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_file(path, json.dumps(data, indent=1, ensure_ascii=False))


def safe_filename(name):
    """Keep only the base name and harmless characters (uploads, archive members)."""
    base = re.split(r"[\\/]", name)[-1]
    base = SAFE_NAME.sub("_", base).strip(". ")
    return base[:120]
