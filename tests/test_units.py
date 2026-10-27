import struct
import zipfile
from datetime import datetime

import pytest

from dwmanager import mods, schedule, worlds
from dwmanager.serverfiles import ServerFiles


def test_cron_basics():
    c = schedule.Cron("0 5 * * *")
    assert c.matches(datetime(2026, 10, 27, 5, 0))
    assert not c.matches(datetime(2026, 10, 27, 5, 1))
    assert schedule.Cron("*/15 * * * *").matches(datetime(2026, 1, 1, 3, 45))
    assert schedule.Cron("0 4 * * mon").matches(datetime(2026, 10, 26, 4, 0))  # a Monday
    assert not schedule.Cron("0 4 * * mon").matches(datetime(2026, 10, 27, 4, 0))
    assert schedule.Cron("0 0 1 * *").next_after(datetime(2026, 10, 27)) == datetime(2026, 11, 1)
    with pytest.raises(schedule.CronError):
        schedule.Cron("61 * * * *")
    with pytest.raises(schedule.CronError):
        schedule.Cron("* * *")
    with pytest.raises(schedule.CronError):
        schedule.validate("0 5 * * *", "explode")


def test_ini_roundtrip(tmp_path):
    files = ServerFiles(tmp_path / "server", tmp_path / "backups")
    files.write_ini({"OwnerId": "abc", "ServerName": "Test", "DefaultWorldName": "W"})
    values, lines = files.read_ini()
    assert values["OwnerId"] == "abc" and lines[1].startswith("[/Script/")
    files.ini.write_text(files.ini.read_text() + 'KnownPlayerList=(UserId=p1,UserName="Bob",Privileges=(PrivilegeMask=14),bIsBanned=False)\n')
    assert files.known_players()[0]["name"] == "Bob"
    files.set_banned("p1", True)
    assert files.known_players()[0]["banned"]
    files.write_ini({"ServerName": "Other"})
    assert files.read_ini()[0]["ServerName"] == "Other" and files.known_players()[0]["banned"]


def test_backup_and_restore(tmp_path):
    files = ServerFiles(tmp_path / "server", tmp_path / "backups", keep=2)
    files.write_ini({"OwnerId": "abc"})
    files.saves_dir.mkdir(parents=True)
    (files.saves_dir / "W.sav").write_bytes(b"SAVE123")
    (files.saved_dir / "Logs").mkdir()
    (files.saved_dir / "Logs/big.log").write_text("x" * 1000)
    target, _ = files.make_backup("test")
    names = zipfile.ZipFile(target).namelist()
    assert any(n.endswith("W.sav") for n in names) and not any("Logs" in n for n in names)
    (files.saves_dir / "W.sav").write_bytes(b"CHANGED")
    files.restore_backup(target)
    assert (files.saves_dir / "W.sav").read_bytes() == b"SAVE123"
    assert (files.saved_dir / "Logs/big.log").exists()
    for _ in range(3):
        files.make_backup("x")
    assert len(files.backups()) == 2


def test_backup_rejects_paths_outside(tmp_path):
    files = ServerFiles(tmp_path / "server", tmp_path / "backups")
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("Saved/../../evil.txt", "x")
        z.writestr("Saved/SaveGames/ok.sav", "SAVE")
    files.restore_backup(bad)
    assert not (tmp_path / "evil.txt").exists() and (files.saves_dir / "ok.sav").exists()


def _fake_save(name, friendly_fire=0, difficulty=0):
    """Minimal SPUD-like blob that world_settings understands."""
    props = [struct.pack("<I", 1), b"\x00" * 16]
    for s in (name, "slot", "L_World"):
        enc = s.encode() + b"\x00"
        props.append(struct.pack("<I", len(enc)) + enc)
    props += [bytes([friendly_fire]), struct.pack("<H", difficulty), struct.pack("<H", 1), struct.pack("<I", 0)]
    offsets, pos = [], 0
    for p in props:
        offsets.append(pos)
        pos += len(p)
    offsets.append(pos)
    body = struct.pack("<I", len(props)) + struct.pack(f"<{len(offsets)}I", *offsets) + b"".join(props)
    return b"SAVE" + b"x" * 12 + b"WorldSaveSettings" + b"PROP" + b"\x00" * 4 + body


def test_worlds_import_and_list(tmp_path):
    saves = tmp_path / "SaveGames"
    data = _fake_save("Looter Paradise3", friendly_fire=1, difficulty=1)
    name = worlds.import_save(saves, data)
    assert name == "Looter Paradise3" and (saves / "Looter Paradise3.sav").exists()
    with pytest.raises(worlds.WorldError):
        worlds.import_save(saves, data)
    rows = worlds.list_worlds(saves, "Looter Paradise3")
    assert rows[0]["active"] and rows[0]["difficulty"] == "Hard" and rows[0]["friendly_fire"]
    rows = worlds.list_worlds(saves, "Fresh")
    assert any(r.get("new") for r in rows)
    with pytest.raises(worlds.WorldError):
        worlds.inner_name(b"not a save")
    z = tmp_path / "w.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("folder/Other.sav", _fake_save("Other"))
    assert worlds.extract_upload("w.zip", z)[0] == "Other"


def test_mods(tmp_path):
    enabled, disabled = tmp_path / "~mods", tmp_path / "mods-disabled"
    z = tmp_path / "m.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for ext in ("pak", "utoc", "ucas"):
            zf.writestr(f"Cool_P/CoolMod_P.{ext}", b"data")
        zf.writestr("readme.txt", "hi")
    assert mods.install_upload("m.zip", z, enabled) == ["CoolMod_P"]
    [m] = mods.list_mods(enabled, disabled)
    assert m["enabled"] and m["complete"]
    mods.set_enabled("CoolMod_P", False, enabled, disabled)
    assert not mods.list_mods(enabled, disabled)[0]["enabled"] and not list(enabled.iterdir())
    mods.delete("CoolMod_P", enabled, disabled)
    assert not mods.list_mods(enabled, disabled)
    lua = tmp_path / "lua.zip"
    with zipfile.ZipFile(lua, "w") as zf:
        zf.writestr("StorageHub/Scripts/main.lua", "x")
    with pytest.raises(mods.ModError, match="ue4ss"):
        mods.install_upload("lua.zip", lua, enabled)


def test_ufw_status_parsing(monkeypatch):
    import subprocess
    from dwmanager import firewall

    class R:
        stdout = "Status: active\n\nTo                         Action      From\n--                         ------      ----\n7777/udp                   ALLOW       Anywhere\n22/tcp                     ALLOW       192.168.0.0/16\n"
    monkeypatch.setattr(firewall, "UFW", "/bin/true")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    assert firewall.ufw_port("status", 7777) == "7777/udp open"
    assert firewall.ufw_port("status", 7778) == "7778/udp closed"
    with pytest.raises(ValueError):
        firewall.ufw_port("status", 70000)
    with pytest.raises(ValueError):
        firewall.ufw_port("explode", 7777)
