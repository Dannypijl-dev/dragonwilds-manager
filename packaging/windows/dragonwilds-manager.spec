# PyInstaller spec: one-file Windows exe with the templates and static files inside.
#   pip install pyinstaller pywin32 && pyinstaller packaging/windows/dragonwilds-manager.spec
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None
a = Analysis(
    ["../../src/dwmanager/__main__.py"],
    pathex=["../../src"],
    datas=[("../../src/dwmanager/templates", "dwmanager/templates"), ("../../src/dwmanager/static", "dwmanager/static")],
    hiddenimports=["win32timezone", "win32serviceutil", "win32service", "win32event", "servicemanager",
                   *collect_submodules("dwmanager")],
    hookspath=[], runtime_hooks=[], excludes=["tkinter"], cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [], name="dragonwilds-manager", console=True,
          icon=None, upx=False, bootloader_ignore_signals=False, disable_windowed_traceback=False)
