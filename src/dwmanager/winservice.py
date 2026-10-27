"""Run the manager as a Windows service (needs pywin32, included in the Windows build).

  dragonwilds-manager.exe service install|start|stop|remove
  dragonwilds-manager.exe service            (what the Service Control Manager runs)
"""
import os
import sys
import threading


def run_service(args):
    try:
        import servicemanager
        import win32event
        import win32service
        import win32serviceutil
    except ImportError:
        print("Windows services need pywin32 (pip install pywin32), or use the Windows build.")
        return 1

    from .__main__ import serve
    from . import platforms

    class Service(win32serviceutil.ServiceFramework):
        _svc_name_ = "DragonwildsManager"
        _svc_display_name_ = "Dragonwilds Manager"
        _svc_description_ = "Web panel that creates and runs RuneScape: Dragonwilds dedicated servers."
        # With the frozen exe the SCM must start this same exe with the "service" argument
        _exe_name_ = sys.executable if platforms.frozen() else None
        _exe_args_ = '"service"' if platforms.frozen() else None

        def __init__(self, a):
            super().__init__(a)
            self.stop_cb = None
            self.stopped = win32event.CreateEvent(None, 0, 0, None)

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=240000)
            if self.stop_cb:
                threading.Thread(target=self.stop_cb, daemon=True).start()
            win32event.SetEvent(self.stopped)

        def SvcDoRun(self):
            servicemanager.LogInfoMsg("Dragonwilds Manager starting")
            host = os.environ.get("DWM_HOST", "0.0.0.0")
            port = int(os.environ.get("DWM_PORT", "8080"))

            def ready(shutdown):
                self.stop_cb = shutdown
            threading.Thread(target=serve, args=(host, port, None, ready), kwargs={"exit_on_stop": False},
                             daemon=True).start()
            win32event.WaitForSingleObject(self.stopped, win32event.INFINITE)

    if not args:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(Service)
        servicemanager.StartServiceCtrlDispatcher()
        return 0
    if args[0] == "install" and len(args) == 1:
        args = ["--startup", "auto", "install"]
    win32serviceutil.HandleCommandLine(Service, argv=[sys.argv[0], *args])
    return 0
