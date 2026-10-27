"""Read and patch the world settings in the server's save file.

The save uses the SPUD format. The first PROP block holds WorldSaveSettings:
a u32 count, count+1 u32 offsets, then the data. Property order (from its CDEF):
  0 WorldSettingsVersion  u32
  1 WorldSaveGuid         16 bytes
  2 WorldName             string (u32 length incl. NUL, bytes)
  3 WorldSlotName         string
  4 WorldMapName          string
  5 bFriendlyFire         u8
  6 SurvivalDifficulty    u16 (enum)
  7 HardcoreState         u16 (enum)
  8 CustomDifficultySettings  array (u32 count first)
Only fixed-size fields are written (friendly fire, difficulty), in place, with the
server stopped; see docs/adr/0006. The custom difficulty array is never touched.
"""
import struct
from pathlib import Path

# Verified on a test server: it logs "Setting Survival Difficulty to ESurvivalDifficulty::<name>".
DIFFICULTY = {0: "Normal", 1: "Hard", 2: "Creative", 3: "Custom"}
SETTABLE_DIFFICULTY = {"Normal": 0, "Hard": 1, "Creative": 2}
HARDCORE = {0: "UNINITIALISED", 1: "Inactive", 2: "Active", 3: "Succeeded", 4: "Failed"}

_cache = {}


def read(save_file):
    path = Path(save_file)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _cache.get(path) and _cache[path][0] == mtime:
        return _cache[path][1]
    try:
        result = _parse(path.read_bytes())
    except (ValueError, IndexError, struct.error):
        result = None
    _cache[path] = (mtime, result)
    return result


def _string(data, pos):
    (length,) = struct.unpack_from("<I", data, pos)
    return data[pos + 4:pos + 4 + length].rstrip(b"\x00").decode("utf-8", "replace")


def _locate(raw):
    """Return a function mapping property index → absolute byte position."""
    if not raw.startswith(b"SAVE"):
        raise ValueError("not a SPUD save")
    anchor = raw.find(b"WorldSaveSettings")
    prop = raw.find(b"PROP", anchor)
    if anchor < 0 or prop < 0:
        raise ValueError("no WorldSaveSettings")
    (count,) = struct.unpack_from("<I", raw, prop + 8)
    if not 9 <= count <= 64:
        raise ValueError("unexpected property count")
    offsets = struct.unpack_from(f"<{count + 1}I", raw, prop + 12)
    base = prop + 12 + 4 * (count + 1)
    # sanity: field sizes this code relies on
    if offsets[6] - offsets[5] != 1 or offsets[7] - offsets[6] != 2 or offsets[8] - offsets[7] != 2:
        raise ValueError("layout changed")
    return lambda i: base + offsets[i]


def write(save_file, friendly_fire, difficulty):
    """Patch friendly fire and difficulty in place. Server must be stopped; caller makes a backup first."""
    if difficulty not in SETTABLE_DIFFICULTY:
        raise ValueError(f"difficulty must be one of {list(SETTABLE_DIFFICULTY)}")
    path = Path(save_file)
    raw = bytearray(path.read_bytes())
    at = _locate(raw)
    raw[at(5)] = 1 if friendly_fire else 0
    struct.pack_into("<H", raw, at(6), SETTABLE_DIFFICULTY[difficulty])
    check = _parse(bytes(raw))
    if check["friendly_fire"] != bool(friendly_fire) or check["difficulty"] != difficulty:
        raise ValueError("verification after patch failed")
    from .util import replace_file
    replace_file(path, raw, mode=path.stat().st_mode & 0o777)
    _cache.pop(path, None)
    return check


def _parse(raw):
    at = _locate(raw)
    world_name = _string(raw, at(2))
    if not world_name:
        raise ValueError("layout changed")
    difficulty = struct.unpack_from("<H", raw, at(6))[0]
    hardcore = struct.unpack_from("<H", raw, at(7))[0]
    (custom_count,) = struct.unpack_from("<I", raw, at(8))
    return {
        "world": world_name,
        "friendly_fire": raw[at(5)] == 1,
        "difficulty": DIFFICULTY.get(difficulty, f"? ({difficulty})"),
        "hardcore": HARDCORE.get(hardcore, f"? ({hardcore})"),
        "custom_count": custom_count,
    }
