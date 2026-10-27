# Dragonwilds Manager

A web panel that creates and runs **RuneScape: Dragonwilds** dedicated servers, in the spirit of Crafty Controller or Pterodactyl but for one game. Install it once, open the panel in a browser, click *New server*: the manager downloads SteamCMD and the server files, starts the server and keeps it running.

Runs on **Windows 10/11**, **Windows Server**, **Linux** (systemd) and in **Docker**.

## What it does

- **Create servers** from the panel: name, port, world password, who can play (crossplay or one platform), your Player ID as owner. The manager downloads the server through SteamCMD (about 5 GB) and starts it.
- **Several servers** on one machine, each on its own UDP port.
- **Start / stop / restart / update**, automatic restart after a crash, clean shutdown (the server saves the world) when the manager or the machine stops.
- **Live console** of the server log, with passwords masked.
- **Players**: who is online with platform and ID, join code of the session, play time per player, **kick** (temporary IP block on the game port), **ban/unban**, a **whitelist** that blocks anyone not on it within seconds of joining.
- **Settings**: server name, world password, platform policy, crash reports, auto start, update on start; **world settings** (friendly fire, difficulty) written into the save.
- **Worlds**: list, switch, import a `.sav` (from another server or a mod site), download, delete.
- **Mods**: upload a zip or `.pak`/`.utoc`/`.ucas` files, enable/disable per mod. Only pak mods load on a dedicated server; the panel explains why a UE4SS mod won't.
- **Backups**: make, restore, upload, download; the newest N are kept.
- **Schedules** (cron): daily backup, restart every 6 hours, update at night, and so on.
- English and Dutch.

## Install

### Windows (PC or Windows Server)

In a PowerShell window started **as Administrator**:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
irm https://raw.githubusercontent.com/Consuelaaa/dragonwilds-manager/main/packaging/windows/install.ps1 | iex
```

This downloads `dragonwilds-manager.exe` from the latest release into `C:\Program Files\DragonwildsManager`, registers the *Dragonwilds Manager* Windows service (automatic start) and opens the panel port in Windows Firewall. Open `http://localhost:8080`. Data lives in `C:\ProgramData\DragonwildsManager`.

Run the same command again to update. `install.ps1 -Uninstall` removes the service and program (data is kept). Without the service: download the exe and run `dragonwilds-manager.exe run`.

### Linux (Debian, Ubuntu, …)

```bash
curl -fsSL https://raw.githubusercontent.com/Consuelaaa/dragonwilds-manager/main/packaging/linux/install.sh | sudo bash
```

Installs the manager into `/opt/dragonwilds-manager`, a `dragonwilds-manager` systemd service running as the unprivileged user `dwmanager`, and a small root helper so kicks and the whitelist can block IPs with nftables. Data lives in `/var/lib/dragonwilds-manager`. Open `http://<host>:8080`. Run the script again to update; `--uninstall` removes the program and keeps the data.

Any distro with Python 3.10+ works by hand: `pip install git+https://github.com/Consuelaaa/dragonwilds-manager && dragonwilds-manager run`. SteamCMD needs the 32-bit C runtime (`lib32gcc-s1 lib32stdc++6` on Debian).

### Docker

```bash
git clone https://github.com/Consuelaaa/dragonwilds-manager && cd dragonwilds-manager
docker compose up -d
```

Panel on port 8080, first game server on UDP 7777, data in the `data` volume. For every extra server add its UDP port to `ports:` in `docker-compose.yml`. Kick/whitelist blocking inside the container needs `cap_add: [NET_ADMIN]` (commented out in the compose file). A ready-made image is published as `ghcr.io/consuelaaa/dragonwilds-manager`.

## First use

1. Open the panel and choose a password.
2. *New server*: fill in your **Player ID** (in the game under Settings, at the bottom, with a copy button; also on consoles). Without an owner the server refuses every player.
3. Wait for the download. The server starts by itself when the owner is set.
4. On your router, forward the server's **UDP port** (7777 by default) to this machine, otherwise only your LAN can join.
5. Players search the **world name** under *Worlds → Public* in the game, or join with the **join code** shown on the overview, and enter the world password.

## Good to know

- The game server has no admin console or RCON. "Kick" therefore blocks the player's IP on the server's port for a chosen time; a ban goes into the server's own player list and takes effect after a restart. Giving items or teleporting is not possible from outside the game.
- Settings and the world's PvP/difficulty are written while the server is stopped (the server rewrites its config on shutdown). The panel stops and starts it for you and makes a backup before touching a save.
- The manager keeps the server matching the game client by running a SteamCMD update before each start (can be turned off per server).
- The world's name lives *inside* the save file; an imported world keeps that name.
- Keep the panel on your LAN or behind a reverse proxy with HTTPS. It has one password, CSRF protection and a login rate limit, nothing more.
- Memory: a server uses about 1.5–2 GB with a few players, more as the world grows. Disk: about 5 GB per server plus backups.
- The panel loads its two typefaces from Google Fonts; without internet access it falls back to system fonts and works the same.

## Development

```bash
pip install -e .[dev]
pytest
DWM_DATA_DIR=./data dragonwilds-manager run --port 8080
```

`src/dwmanager/instance.py` runs one server (SteamCMD, process, log), `manager.py` holds all of them plus the scheduler, `web.py` is the Flask panel. Platform differences are in `platforms.py` and `firewall.py`. Windows builds come from `packaging/windows/dragonwilds-manager.spec` (PyInstaller) and are published by the release workflow.

## License

MIT. Not affiliated with Jagex; RuneScape and RuneScape: Dragonwilds are trademarks of Jagex Ltd.
