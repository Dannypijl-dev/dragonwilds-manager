"""The web panel."""
import hmac
import logging
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request, send_file, session,
                   url_for)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

from . import __version__, mods, schedule, world_settings, worlds
from .history import parse_iso
from .i18n import LANGUAGES, translator
from .instance import InstanceBusy
from .serverfiles import POLICIES

log = logging.getLogger("dwmanager.web")
KICK_MINUTES = [15, 60, 1440]
PLAYER_ID = re.compile(r"^[A-Za-z0-9\-]{8,64}$")
FIELD_RULES = {
    "ServerName": r"^[^\r\n=\[\]]{3,64}$",
    "DefaultWorldName": r"^[A-Za-z0-9 _'\-]{1,32}$",
    "WorldPassword": r"^\S{0,64}$",
    "OwnerId": r"^[A-Za-z0-9\-]{0,64}$",
}
TABS = ["overview", "players", "settings", "worlds", "mods", "backups", "schedules"]


def create_app(manager):
    app = Flask(__name__)
    app.secret_key = manager.settings["secret_key"]
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                      PERMANENT_SESSION_LIFETIME=7 * 86400, MAX_CONTENT_LENGTH=8 * 1024**3)
    attempts = {}

    def lang():
        return manager.settings.get("lang", "en")

    def t(key, **kw):
        return translator(lang())(key, **kw)

    @app.context_processor
    def inject():
        return {"t": lambda key, **kw: Markup(t(key, **kw)), "lang": lang(), "version": __version__,
                "languages": LANGUAGES, "policies": POLICIES, "servers": list(manager.instances.values())}

    # ---------- auth ----------
    @app.before_request
    def guard():
        if request.endpoint in ("static", "health"):
            return None
        if not manager.settings.get("password_hash"):
            return None if request.endpoint == "setup" else redirect(url_for("setup"))
        if request.endpoint in ("login", "setup"):
            return None
        if not session.get("ok"):
            if request.path.startswith("/api/"):
                abort(401)
            return redirect(url_for("login"))
        if request.method == "POST" and not hmac.compare_digest(request.form.get("csrf", ""), session.get("csrf", "")):
            abort(400)
        return None

    def login_session():
        session.clear()
        session.permanent = True
        session.update(ok=True, csrf=secrets.token_urlsafe(32))

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if manager.settings.get("password_hash"):
            return redirect(url_for("login"))
        if request.method == "POST":
            pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
            if len(pw) < 8:
                flash(t("pw_short"), "error")
            elif pw != pw2:
                flash(t("pw_mismatch"), "error")
            else:
                manager.settings["password_hash"] = generate_password_hash(pw)
                if request.form.get("lang") in LANGUAGES:
                    manager.settings["lang"] = request.form["lang"]
                manager.save_settings()
                login_session()
                return redirect(url_for("dashboard"))
        return render_template("auth.html", view="setup")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            ip = request.remote_addr or "?"
            recent = [x for x in attempts.get(ip, []) if time.time() - x < 600]
            if len(recent) >= 10:
                flash(t("login_locked"), "error")
                return render_template("auth.html", view="login"), 429
            if check_password_hash(manager.settings["password_hash"], request.form.get("password", "")):
                attempts.pop(ip, None)
                login_session()
                return redirect(request.args.get("next") or url_for("dashboard"))
            attempts[ip] = recent + [time.time()]
            time.sleep(1.5)
            flash(t("login_wrong"), "error")
        return render_template("auth.html", view="login")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/healthz")
    def health():
        return "ok"

    # ---------- helpers ----------
    def inst_or_404(iid):
        try:
            return manager.get(iid)
        except KeyError:
            abort(404)

    def back(inst, tab="overview"):
        return redirect(url_for("server", iid=inst.id, tab=tab))

    def action(tab):
        """Route decorator: look up the instance, turn known errors into flash messages, redirect back."""
        def deco(func):
            @wraps(func)
            def wrapper(iid, *a, **kw):
                inst = inst_or_404(iid)
                try:
                    result = func(inst, *a, **kw)
                    if result is not None:
                        return result
                except InstanceBusy as exc:
                    flash(t("busy", task=t(f"state_{exc}")), "error")
                except (ValueError, RuntimeError, FileNotFoundError) as exc:
                    log.warning("%s: %s failed: %s", inst.id, func.__name__, exc)
                    key = f"err_{exc}"
                    flash(t(key) if t(key) != key else t("failed", error=exc), "error")
                return back(inst, request.args.get("tab", tab))
            return wrapper
        return deco

    def status(inst):
        values = inst.files.read_ini()[0]
        running = inst.running()
        plist, events = inst.watcher.players() if running else ([], [])
        owner = values.get("OwnerId", "")
        owner_ips = {p["ip"] for p in plist if p["id"] == owner and p.get("ip")}
        for p in plist:
            p["is_owner"] = bool(owner) and p["id"] == owner
            p["shared_with"] = [o["name"] for o in plist if o is not p and o.get("ip") and o.get("ip") == p.get("ip")]
            p["kickable"] = manager.firewall.available and bool(p.get("ip")) and p["ip"] not in owner_ips
        mem = inst.memory() if running else None
        return {
            "id": inst.id, "name": inst.config["name"], "state": inst.state(), "progress": inst.progress,
            "message": inst.message, "port": inst.port, "running": running,
            "since": inst.started_at.strftime("%d-%m-%Y %H:%M") if running and inst.started_at else "",
            "memory": f"{mem / 1024**3:.1f} GB" if mem else "–",
            "join_code": inst.watcher.join_code if running else None,
            "world": values.get("DefaultWorldName", ""), "server_name": values.get("ServerName", ""),
            "owner_set": bool(owner), "players": plist,
            "events": [f"{'➜' if kind == 'join' else '✕'} {who} {t('joined' if kind == 'join' else 'left')}"
                       for kind, who in events],
            "blocked": [{"ip": b["ip"], "minutes": max(1, round(b["seconds"] / 60))}
                        for b in manager.firewall.blocked(inst.port)],
            "can_block": manager.firewall.available, "build": _build(inst),
            "host_firewall": manager.firewall.port_mode,
            "port_open": manager.firewall.port_is_open(inst.port) if manager.firewall.port_mode else None,
        }

    # ---------- pages ----------
    @app.get("/")
    def dashboard():
        return render_template("dashboard.html", rows=[status(i) for i in manager.instances.values()])

    @app.get("/api/servers")
    def api_servers():
        return jsonify([status(i) for i in manager.instances.values()])

    @app.route("/new", methods=["GET", "POST"])
    def new_server():
        form = {"name": "", "port": manager.free_port(), "ServerName": "", "DefaultWorldName": "",
                "WorldPassword": "", "OwnerId": "", "PlatformPolicy": "Crossplay"}
        if request.method == "POST":
            form.update({k: request.form.get(k, "").strip() for k in form})
            errors = []
            if not 3 <= len(form["name"]) <= 40:
                errors.append(t("v_name"))
            if not str(form["port"]).isdigit() or not 1024 <= int(form["port"]) <= 65535:
                errors.append(t("v_port"))
            elif any(i.port == int(form["port"]) for i in manager.instances.values()):
                errors.append(t("err_port_in_use"))
            form["ServerName"] = form["ServerName"] or form["name"]
            form["DefaultWorldName"] = form["DefaultWorldName"] or re.sub(r"[^A-Za-z0-9 _'\-]", "", form["name"])[:32] or "World"
            for key, rx in FIELD_RULES.items():
                if not re.match(rx, form[key]):
                    errors.append(t(f"v_{key}"))
            if form["PlatformPolicy"] not in POLICIES:
                abort(400)
            if errors:
                for e in errors:
                    flash(e, "error")
            else:
                ini = {k: form[k] for k in ("ServerName", "DefaultWorldName", "WorldPassword", "OwnerId", "PlatformPolicy")}
                inst = manager.create(form["name"], int(form["port"]), ini)
                flash(t("created", name=form["name"]), "ok")
                return redirect(url_for("server", iid=inst.id))
        return render_template("new.html", form=form)

    @app.get("/s/<iid>")
    def server(iid):
        inst = inst_or_404(iid)
        tab = request.args.get("tab", "overview")
        if tab not in TABS:
            tab = "overview"
        values = inst.files.read_ini()[0]
        ctx = {"inst": inst, "s": status(inst), "v": values, "tab": tab, "tabs": TABS, "cfg": inst.config}
        if tab == "players":
            ctx.update(roster=roster(inst, values.get("OwnerId", "")), kick_minutes=KICK_MINUTES)
        elif tab == "settings":
            ctx.update(world=inst.world_info(), difficulties=list(world_settings.SETTABLE_DIFFICULTY))
        elif tab == "worlds":
            ctx.update(worlds=inst.list_worlds())
        elif tab == "mods":
            ctx.update(mods=inst.list_mods())
        elif tab == "backups":
            ctx.update(backups=[{"name": b["name"], "date": b["date"].strftime("%d-%m-%Y %H:%M"),
                                 "size": _size(b["size"])} for b in inst.files.backups()])
        elif tab == "schedules":
            ctx.update(schedules=[{**s, "next": _fmt(schedule.next_run(s["cron"])) if s.get("enabled") else "–",
                                   "last": _fmt_iso(s.get("last"))} for s in inst.config.get("schedules", [])],
                       presets=schedule.PRESETS, actions=schedule.ACTIONS)
        return render_template("server.html", **ctx)

    def roster(inst, owner):
        h = inst.history.snapshot()
        allowed = set(h["whitelist"]["ids"])
        known = {k["id"]: k for k in inst.files.known_players()}
        rows = []
        for pid in dict.fromkeys([*h["players"], *known, *allowed]):
            p, k = h["players"].get(pid, {}), known.get(pid, {})
            rows.append({"id": pid, "name": p.get("name") or k.get("name") or "?", "platform": p.get("platform"),
                         "seconds": p.get("seconds", 0), "time": _duration(p.get("seconds", 0), t) if p else "–",
                         "sessions": p.get("sessions", 0), "last": _local(p.get("last")) if p else "–",
                         "online": p.get("online", False), "is_owner": pid == owner, "known": pid in known,
                         "banned": k.get("banned", False), "allowed": pid in allowed or pid == owner})
        rows.sort(key=lambda r: (not r["is_owner"], -r["seconds"], r["name"].lower()))
        names = {r["id"]: r["name"] for r in rows}
        sessions = [{"name": names.get(x["id"], x["name"]), "start": _local(x["start"]), "time": _duration(x["seconds"], t),
                     "open": x.get("open", False)} for x in reversed(h["sessions"][-12:])]
        sessions.sort(key=lambda x: not x["open"])
        refused = [{**r, "at": _local(r["at"])} for r in reversed(h["refused"][-6:])]
        return {"rows": rows, "sessions": sessions, "refused": refused, "whitelist": h["whitelist"]["enabled"],
                "can_block": manager.firewall.available, "block_hours": round(int(inst.config["block_minutes"]) / 60)}

    @app.get("/api/s/<iid>/status")
    def api_status(iid):
        return jsonify(status(inst_or_404(iid)))

    @app.get("/api/s/<iid>/console")
    def api_console(iid):
        inst = inst_or_404(iid)
        after = request.args.get("after", "0")
        rows, seq = inst.watcher.tail(int(after) if after.isdigit() else 0)
        return jsonify({"seq": seq, "lines": [l for _, l in rows]})

    # ---------- control ----------
    @app.post("/s/<iid>/control/<what>")
    @action("overview")
    def control(inst, what):
        if what == "start":
            inst.start()
            flash(t("f_starting"), "ok")
        elif what == "stop":
            inst.stop_async()
            flash(t("f_stopping"), "ok")
        elif what == "restart":
            inst.restart_async()
            flash(t("f_restarting"), "ok")
        elif what == "update":
            inst.update()
            flash(t("f_updating"), "ok")
        elif what == "install":
            inst.install()
            flash(t("f_installing"), "ok")
        else:
            abort(404)

    @app.post("/s/<iid>/port/<what>")
    @action("settings")
    def host_port(inst, what):
        if what not in ("open", "close"):
            abort(404)
        manager.firewall.set_port(inst.port, what == "open")
        log.info("%s: UDP %s %s in the host firewall", inst.id, inst.port, "opened" if what == "open" else "closed")
        flash(t("f_port_opened" if what == "open" else "f_port_closed", port=inst.port), "ok")

    @app.post("/s/<iid>/delete")
    def delete_server(iid):
        inst = inst_or_404(iid)
        if request.form.get("confirm", "") != inst.config["name"]:
            flash(t("delete_confirm_wrong"), "error")
            return back(inst, "settings")
        try:
            manager.delete(iid)
        except InstanceBusy as exc:
            flash(t("busy", task=t(f"state_{exc}")), "error")
            return back(inst, "settings")
        flash(t("deleted", name=inst.config["name"]), "ok")
        return redirect(url_for("dashboard"))

    # ---------- settings ----------
    @app.post("/s/<iid>/settings")
    @action("settings")
    def settings(inst):
        updates = {}
        for key, rx in FIELD_RULES.items():
            value = request.form.get(key, "").strip()
            if not re.match(rx, value):
                flash(t(f"v_{key}"), "error")
                return back(inst, "settings")
            updates[key] = value
        policy = request.form.get("PlatformPolicy", "Crossplay")
        if policy not in POLICIES:
            abort(400)
        updates["PlatformPolicy"] = policy
        updates["bAllowSendingCrashDumps"] = "True" if request.form.get("bAllowSendingCrashDumps") else "False"

        name = request.form.get("name", "").strip()
        port = request.form.get("port", "")
        keep = request.form.get("backup_keep", "")
        if not 3 <= len(name) <= 40:
            raise ValueError("v_name")
        if not port.isdigit() or not 1024 <= int(port) <= 65535:
            raise ValueError("v_port")
        if any(i.port == int(port) and i is not inst for i in manager.instances.values()):
            raise ValueError("port_in_use")
        if not keep.isdigit() or not 1 <= int(keep) <= 500:
            raise ValueError("v_keep")
        port_changed = int(port) != inst.port
        inst.config.update(name=name, port=int(port), backup_keep=int(keep),
                           auto_start=bool(request.form.get("auto_start")),
                           auto_restart=bool(request.form.get("auto_restart")),
                           update_on_start=bool(request.form.get("update_on_start")))
        inst.save_config()

        old = inst.files.read_ini()[0]
        if all(old.get(k, "") == v for k, v in updates.items()) and not port_changed:
            flash(t("f_saved"), "ok")
            return None
        first_owner = bool(updates["OwnerId"]) and not old.get("OwnerId")
        restarted = inst.with_stopped(lambda: inst.files.write_ini(updates))
        if first_owner and not inst.running() and inst.installed:
            inst.start()
            restarted = True
        flash(t("f_saved_restart") if restarted else t("f_saved"), "ok")
        if not updates["OwnerId"]:
            flash(t("f_owner_empty"), "error")

    @app.post("/s/<iid>/world-settings")
    @action("settings")
    def world_settings_route(inst):
        current = inst.world_info()
        if current is None:
            raise ValueError("ws_unknown")
        friendly_fire = request.form.get("friendly_fire") == "on"
        difficulty = request.form.get("difficulty", "")
        if current["difficulty"] not in world_settings.SETTABLE_DIFFICULTY and difficulty == current["difficulty"]:
            difficulty = None
        elif difficulty not in world_settings.SETTABLE_DIFFICULTY:
            abort(400)
        if current["friendly_fire"] == friendly_fire and difficulty in (None, current["difficulty"]):
            flash(t("f_unchanged"), "info")
            return None
        save = inst.files.saves_dir / f"{inst.files.read_ini()[0].get('DefaultWorldName', '')}.sav"
        restarted = inst.with_stopped(lambda: world_settings.write(save, friendly_fire, difficulty or current["difficulty"]),
                                      backup=True)
        flash(t("f_world_saved") + (" " + t("f_restarting") if restarted else ""), "ok")

    # ---------- worlds ----------
    def upload_to_temp(field):
        f = request.files.get(field)
        if not f or not f.filename:
            raise ValueError("no_file")
        tmp = Path(tempfile.mkdtemp(prefix="dwm-")) / "upload"
        f.save(tmp)
        return f.filename, tmp

    @app.post("/s/<iid>/worlds/upload")
    @action("worlds")
    def world_upload(inst):
        filename, tmp = upload_to_temp("file")
        try:
            name, data = worlds.extract_upload(filename, tmp)
        finally:
            _cleanup(tmp)
        overwrite = request.form.get("overwrite") == "1"
        activate = request.form.get("activate") == "1"

        def change():
            worlds.import_save(inst.files.saves_dir, data, overwrite=overwrite)
            if activate:
                inst.files.write_ini({"DefaultWorldName": name})
        if inst.running() or activate or overwrite:
            restarted = inst.with_stopped(change, backup=True)
        else:
            change()
            restarted = False
        flash(t("f_world_imported", name=name) + (" " + t("f_restarting") if restarted else ""), "ok")

    @app.post("/s/<iid>/worlds/activate")
    @action("worlds")
    def world_activate(inst):
        name = request.form.get("name", "").strip()
        if not re.match(FIELD_RULES["DefaultWorldName"], name) and request.form.get("file") is None:
            raise ValueError("v_DefaultWorldName")
        if file := request.form.get("file"):
            name = worlds.inner_name(worlds.save_file(inst.files.saves_dir, file))
        restarted = inst.with_stopped(lambda: inst.files.write_ini({"DefaultWorldName": name}))
        flash(t("f_world_active", name=name) + (" " + t("f_restarting") if restarted else ""), "ok")

    @app.post("/s/<iid>/worlds/delete")
    @action("worlds")
    def world_delete(inst):
        file = request.form.get("file", "")
        path = worlds.save_file(inst.files.saves_dir, file)
        if worlds.inner_name(path) == inst.files.read_ini()[0].get("DefaultWorldName"):
            raise ValueError("world_active")
        inst.files.make_backup("before-delete")
        worlds.delete(inst.files.saves_dir, file)
        flash(t("f_world_deleted", name=Path(file).stem), "ok")

    @app.get("/s/<iid>/worlds/download/<path:file>")
    def world_download(iid, file):
        inst = inst_or_404(iid)
        try:
            return send_file(worlds.save_file(inst.files.saves_dir, file), as_attachment=True)
        except FileNotFoundError:
            abort(404)

    # ---------- mods ----------
    @app.post("/s/<iid>/mods/upload")
    @action("mods")
    def mod_upload(inst):
        filename, tmp = upload_to_temp("file")
        try:
            names = mods.install_upload(filename, tmp, inst.files.mods_dir)
        except mods.ModError as exc:
            raise ValueError(f"mod_{exc}") from exc
        finally:
            _cleanup(tmp)
        flash(t("f_mod_installed", names=", ".join(names)) + (" " + t("f_mod_restart") if inst.running() else ""), "ok")

    @app.post("/s/<iid>/mods/<what>")
    @action("mods")
    def mod_change(inst, what):
        name = request.form.get("name", "")
        try:
            if what == "enable":
                mods.set_enabled(name, True, inst.files.mods_dir, inst.mods_disabled_dir)
            elif what == "disable":
                mods.set_enabled(name, False, inst.files.mods_dir, inst.mods_disabled_dir)
            elif what == "delete":
                mods.delete(name, inst.files.mods_dir, inst.mods_disabled_dir)
            else:
                abort(404)
        except mods.ModError as exc:
            raise ValueError(f"mod_{exc}") from exc
        flash(t(f"f_mod_{what}d", name=name) + (" " + t("f_mod_restart") if inst.running() else ""), "ok")

    # ---------- backups ----------
    @app.post("/s/<iid>/backups/create")
    @action("backups")
    def backup_create(inst):
        hot = request.form.get("hot") == "1"
        if hot or not inst.running():
            target, _ = inst.files.make_backup("manual")
            flash(t("f_backup", name=target.name), "ok")
        else:
            inst.with_stopped(lambda: None, backup=True)
            flash(t("f_backup_cycle"), "ok")

    @app.post("/s/<iid>/backups/restore")
    @action("backups")
    def backup_restore(inst):
        archive = inst.files.backup_path(request.form.get("name", ""))

        def change():
            inst.files.restore_backup(archive)
        restarted = inst.with_stopped(change, backup=True)
        flash(t("f_restored", name=archive.name) + (" " + t("f_restarting") if restarted else ""), "ok")

    @app.post("/s/<iid>/backups/upload")
    @action("backups")
    def backup_upload(inst):
        filename, tmp = upload_to_temp("file")
        if not filename.lower().endswith((".zip", ".tar.gz", ".tgz")):
            _cleanup(tmp)
            raise ValueError("backup_type")
        suffix = ".zip" if filename.lower().endswith(".zip") else ".tar.gz"
        archive = tmp.with_name("upload" + suffix)
        tmp.rename(archive)
        try:
            restarted = inst.with_stopped(lambda: inst.files.restore_backup(archive), backup=True)
        finally:
            _cleanup(archive)
        flash(t("f_restored", name=filename) + (" " + t("f_restarting") if restarted else ""), "ok")

    @app.post("/s/<iid>/backups/delete")
    @action("backups")
    def backup_delete(inst):
        archive = inst.files.backup_path(request.form.get("name", ""))
        archive.unlink()
        flash(t("f_backup_deleted", name=archive.name), "ok")

    @app.get("/s/<iid>/backups/download/<name>")
    def backup_download(iid, name):
        inst = inst_or_404(iid)
        try:
            return send_file(inst.files.backup_path(name), as_attachment=True)
        except FileNotFoundError:
            abort(404)

    # ---------- schedules ----------
    @app.post("/s/<iid>/schedules/add")
    @action("schedules")
    def schedule_add(inst):
        expr = request.form.get("cron", "").strip() or request.form.get("preset", "")
        what = request.form.get("action", "")
        try:
            expr = schedule.validate(expr, what)
        except schedule.CronError as exc:
            flash(t("v_cron", error=exc), "error")
            return None
        inst.config.setdefault("schedules", []).append({"id": secrets.token_hex(4), "cron": expr, "action": what,
                                                        "enabled": True})
        inst.save_config()
        flash(t("f_schedule_added"), "ok")

    @app.post("/s/<iid>/schedules/<what>")
    @action("schedules")
    def schedule_change(inst, what):
        sid = request.form.get("id", "")
        task = next((s for s in inst.config.get("schedules", []) if s["id"] == sid), None)
        if not task:
            abort(404)
        if what == "toggle":
            task["enabled"] = not task.get("enabled")
        elif what == "delete":
            inst.config["schedules"].remove(task)
        elif what == "run":
            threading.Thread(target=manager.run_task, args=(inst, task), daemon=True).start()
            flash(t("f_schedule_running"), "ok")
        else:
            abort(404)
        inst.save_config()

    # ---------- players ----------
    @app.post("/s/<iid>/kick")
    @action("players")
    def kick(inst):
        name, minutes = request.form.get("name", ""), request.form.get("minutes", "15")
        if not minutes.isdigit() or int(minutes) not in KICK_MINUTES:
            abort(400)
        player = next((p for p in status(inst)["players"] if p["name"] == name), None)
        if not player or not player.get("ip"):
            flash(t("f_kick_unknown", name=name), "error")
        elif player["is_owner"]:
            flash(t("f_kick_self"), "error")
        elif not player["kickable"]:
            flash(t("f_kick_owner", name=name), "error")
        else:
            manager.firewall.block(player["ip"], inst.port, int(minutes))
            flash(t("f_kicked", name=name, minutes=minutes), "ok")
            if player["shared_with"]:
                flash(t("f_kick_shared", names=", ".join(player["shared_with"])), "info")

    @app.post("/s/<iid>/unblock")
    @action("players")
    def unblock(inst):
        ip = request.form.get("ip", "")
        if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
            abort(400)
        manager.firewall.unblock(ip, inst.port)
        flash(t("f_unblocked", ip=ip), "ok")

    @app.post("/s/<iid>/ban")
    @action("players")
    def ban(inst):
        user_id, banned = request.form.get("id", ""), request.form.get("banned") == "1"
        owner = inst.files.read_ini()[0].get("OwnerId", "")
        player = next((k for k in inst.files.known_players() if k["id"] == user_id), None)
        if not player:
            abort(400)
        if banned and user_id == owner:
            flash(t("f_kick_self"), "error")
            return None
        restarted = inst.with_stopped(lambda: inst.files.set_banned(user_id, banned))
        flash(t("f_banned" if banned else "f_unbanned", name=player["name"])
              + (" " + t("f_restarting") if restarted else ""), "ok")

    @app.post("/s/<iid>/whitelist")
    @action("players")
    def whitelist(inst):
        enabled = request.form.get("enabled") == "1"
        inst.history.set_whitelist_enabled(enabled)
        flash(t("f_wl_on" if enabled else "f_wl_off"), "ok")

    @app.post("/s/<iid>/whitelist/player")
    @action("players")
    def whitelist_player(inst):
        pid, allowed = request.form.get("id", "").strip(), request.form.get("allowed") == "1"
        if not PLAYER_ID.match(pid):
            flash(t("v_player_id"), "error")
            return None
        inst.history.set_allowed(pid, allowed)
        flash(t("f_wl_allowed" if allowed else "f_wl_removed", id=pid), "ok")

    # ---------- manager settings ----------
    @app.route("/manager", methods=["GET", "POST"])
    def manager_settings():
        if request.method == "POST":
            what = request.form.get("what")
            if what == "lang" and request.form.get("lang") in LANGUAGES:
                manager.settings["lang"] = request.form["lang"]
                manager.save_settings()
                flash(t("f_saved"), "ok")
            elif what == "password":
                if not check_password_hash(manager.settings["password_hash"], request.form.get("current", "")):
                    flash(t("login_wrong"), "error")
                elif len(request.form.get("password", "")) < 8:
                    flash(t("pw_short"), "error")
                elif request.form.get("password") != request.form.get("password2"):
                    flash(t("pw_mismatch"), "error")
                else:
                    manager.settings["password_hash"] = generate_password_hash(request.form["password"])
                    manager.settings["secret_key"] = secrets.token_hex(32)  # log out other sessions
                    manager.save_settings()
                    app.secret_key = manager.settings["secret_key"]
                    login_session()
                    flash(t("f_pw_changed"), "ok")
            return redirect(url_for("manager_settings"))
        return render_template("manager.html", data_dir=str(manager.data_dir), firewall=manager.firewall.mode)

    return app


def _build(inst):
    from . import steamcmd
    return steamcmd.build_id(inst.server_dir) or "–"


def _cleanup(path):
    import shutil
    shutil.rmtree(Path(path).parent, ignore_errors=True)


def _size(n):
    return f"{n / 1024**2:.1f} MB" if n < 1024**3 else f"{n / 1024**3:.2f} GB"


def _local(iso):
    if not iso:
        return "–"
    dt = parse_iso(iso).astimezone()
    return dt.strftime("%d-%m %H:%M" if dt.year == datetime.now().year else "%d-%m-%Y %H:%M")


def _fmt(dt):
    return dt.strftime("%a %d-%m %H:%M") if dt else "–"


def _fmt_iso(iso):
    try:
        return _fmt(datetime.fromisoformat(iso)) if iso else "–"
    except ValueError:
        return "–"


def _duration(seconds, t):
    minutes = round(seconds / 60)
    if minutes < 60:
        return t("dur_m", m=minutes)
    return t("dur_hm", h=minutes // 60, m=minutes % 60)
