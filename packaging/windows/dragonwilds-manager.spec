# PyInstaller spec: two one-file Windows exes with the templates and static files inside.
#   pip install .[desktop] pyinstaller pywin32 && pyinstaller packaging/windows/dragonwilds-manager.spec
#   dragonwilds-manager.exe      console: the service, `run`, and the ctrl-c/firewall helpers
#   dragonwilds-manager-app.exe  no console: what the Start Menu shortcut runs (`app`, the panel in a window)
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None
icon = "../../src/dwmanager/static/dragonwilds-manager.ico"
a = Analysis(
    ["launcher.py"],
    pathex=["../../src"],
    datas=[("../../src/dwmanager/templates", "dwmanager/templates"), ("../../src/dwmanager/static", "dwmanager/static"),
           # pywebview brings its own hook for webview/lib; pythonnet and clr_loader need theirs (.NET runtime files)
           *collect_data_files("pythonnet"), *collect_data_files("clr_loader")],
    hiddenimports=["win32timezone", "win32serviceutil", "win32service", "win32event", "servicemanager",
                   "clr", "clr_loader", "pythonnet", "webview.platforms.winforms", "webview.platforms.edgechromium",
                   *collect_submodules("dwmanager"), *collect_submodules("webview")],
    hookspath=[], runtime_hooks=[], excludes=["tkinter"], cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [], name="dragonwilds-manager", console=True,
          icon=icon, upx=False, bootloader_ignore_signals=False, disable_windowed_traceback=False)
app = EXE(pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [], name="dragonwilds-manager-app", console=False,
          icon=icon, upx=False, bootloader_ignore_signals=False, disable_windowed_traceback=False)
