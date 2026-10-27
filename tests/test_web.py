import re
from pathlib import Path

import pytest

from dwmanager import instance, steamcmd
from dwmanager.manager import Manager
from dwmanager.web import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DWM_FIREWALL", "none")
    def fake_install(steamcmd_dir, server_dir, *a, **k):
        # no real download in tests: a stand-in binary that exits right away
        exe = Path(server_dir) / "RSDragonwilds/Binaries/Linux/RSDragonwildsServer-Linux-Shipping"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
        return True
    monkeypatch.setattr(steamcmd, "install_server", fake_install)
    monkeypatch.setattr(instance.platforms, "WINDOWS", False)
    m = Manager(tmp_path / "data")
    app = create_app(m)
    app.config["TESTING"] = True
    c = app.test_client()
    c.manager = m
    return c


def csrf(c):
    html = c.get("/").get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_setup_login_and_pages(client):
    assert client.get("/").status_code == 302  # to /setup
    r = client.post("/setup", data={"password": "secret123", "password2": "secret123", "lang": "nl"}, follow_redirects=True)
    assert "Nog geen servers" in r.get_data(as_text=True)
    client.post("/logout", data={"csrf": csrf(client)})
    assert client.get("/").status_code == 302
    r = client.post("/login", data={"password": "wrong"})
    assert "Verkeerd" in r.get_data(as_text=True)
    r = client.post("/login", data={"password": "secret123"}, follow_redirects=True)
    assert "Nog geen servers" in r.get_data(as_text=True)
    assert client.get("/new").status_code == 200
    assert client.get("/manager").status_code == 200


def test_create_server_and_tabs(client):
    client.post("/setup", data={"password": "secret123", "password2": "secret123", "lang": "en"})
    token = csrf(client)
    r = client.post("/new", data={"csrf": token, "name": "My server", "port": "7777", "ServerName": "", "DefaultWorldName": "",
                                 "WorldPassword": "pw", "OwnerId": "0002abc", "PlatformPolicy": "Crossplay"}, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "is being installed" in html
    [inst] = client.manager.instances.values()
    assert inst.port == 7777 and inst.config["name"] == "My server"
    # port clash
    r = client.post("/new", data={"csrf": token, "name": "Second", "port": "7777", "ServerName": "", "DefaultWorldName": "",
                                 "WorldPassword": "", "OwnerId": "", "PlatformPolicy": "PC"})
    assert "already used" in r.get_data(as_text=True)
    import time
    for _ in range(50):
        if not inst.task:
            break
        time.sleep(0.05)
    assert inst.files.read_ini()[0]["OwnerId"] == "0002abc"
    for tab in ("overview", "players", "settings", "worlds", "mods", "backups", "schedules"):
        assert client.get(f"/s/{inst.id}?tab={tab}").status_code == 200, tab
    assert client.get(f"/api/s/{inst.id}/status").get_json()["state"] in ("stopped", "starting", "updating")
    assert client.get(f"/api/s/{inst.id}/console").get_json()["lines"] == []
    # schedules
    r = client.post(f"/s/{inst.id}/schedules/add", data={"csrf": token, "action": "backup", "preset": "0 */6 * * *", "cron": ""}, follow_redirects=True)
    assert "0 */6 * * *" in r.get_data(as_text=True)
    r = client.post(f"/s/{inst.id}/schedules/add", data={"csrf": token, "action": "backup", "preset": "", "cron": "bad"}, follow_redirects=True)
    assert "Not a valid schedule" in r.get_data(as_text=True)
    # settings save (server not installed, so no restart)
    r = client.post(f"/s/{inst.id}/settings", data={"csrf": token, "OwnerId": "0002abc", "ServerName": "Renamed", "DefaultWorldName": "World",
                                                  "WorldPassword": "", "PlatformPolicy": "PC", "name": "My server", "port": "7778",
                                                  "backup_keep": "5", "auto_start": "on"}, follow_redirects=True)
    assert inst.files.read_ini()[0]["ServerName"] == "Renamed" and inst.port == 7778 and inst.config["backup_keep"] == 5
    # whitelist
    client.post(f"/s/{inst.id}/whitelist/player", data={"csrf": token, "id": "0002friend99", "allowed": "1"})
    assert "0002friend99" in inst.history.whitelist()["ids"]
    # backup without server files
    r = client.post(f"/s/{inst.id}/backups/create", data={"csrf": token, "hot": "1"}, follow_redirects=True)
    assert "Backup made" in r.get_data(as_text=True)
    # delete needs the exact name
    r = client.post(f"/s/{inst.id}/delete", data={"csrf": token, "confirm": "nope"}, follow_redirects=True)
    assert inst.id in client.manager.instances
    client.post(f"/s/{inst.id}/delete", data={"csrf": token, "confirm": "My server"})
    assert not client.manager.instances and not inst.root.exists()


def test_csrf_required(client):
    client.post("/setup", data={"password": "secret123", "password2": "secret123"})
    assert client.post("/new", data={"name": "x"}).status_code == 400
