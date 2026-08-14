from __future__ import annotations

import asyncio
import os
import sys
import threading
from contextlib import suppress
from pathlib import Path

from .config import ConfigError, load_config
from .core.outbox import EventOutbox
from .logsetup import configure_logging
from .uplink.connection import HubConnection
from .updates import apply_update, check_for_update

try:  # pragma: no cover - Windows-specific dependency
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil
except ImportError as exc:  # pragma: no cover - only exercised on systems without pywin32
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

SERVICE_NAME = "printer-agent"
SERVICE_DISPLAY_NAME = "printer-agent"
SERVICE_DESCRIPTION = "Edge agent for a distributed 3D printer fleet"
CONFIG_DIR = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "printer-agent"
CONFIG_PATH = CONFIG_DIR / "agent.yaml"

SERVICE_KEY = rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}"


def service_environment_paths() -> list[Path]:
    """Directories pythonservice.exe must have on PATH to start at all.

    It lives in the venv root and links against python3XX.dll, which is in the
    *base* interpreter — a venv does not copy it. Without these the process dies
    with 0xC0000135 (DLL not found) before reaching any of our code, and the SCM
    reports only "did not respond in a timely fashion".
    """
    import sysconfig

    site_packages = Path(sysconfig.get_paths()["purelib"])
    return [
        Path(sys.base_prefix),  # python3XX.dll
        Path(sys.base_prefix) / "DLLs",
        site_packages / "pywin32_system32",  # pywintypes/pythoncom
        Path(sys.prefix) / "Scripts",
    ]


def build_service_environment() -> list[str]:
    """The REG_MULTI_SZ block Windows hands the service process."""
    import sysconfig

    existing = os.environ.get("PATH", "")
    prefix = os.pathsep.join(str(path) for path in service_environment_paths())
    return [
        f"PATH={prefix}{os.pathsep}{existing}" if existing else f"PATH={prefix}",
        # pythonservice.exe puts the service module's own directory on sys.path,
        # not site-packages, so `import printer_agent` would fail without this.
        f"PYTHONPATH={sysconfig.get_paths()['purelib']}",
    ]


def configure_service_environment() -> None:
    """Write the environment onto the service key. Requires administrator."""
    if os.name != "nt":
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, SERVICE_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "Environment", 0, winreg.REG_MULTI_SZ, build_service_environment())


SERVICE_MODULE = "printer_agent.windows_service"


if _IMPORT_ERROR is None:
    class PrinterAgentService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        # Run the venv's own python.exe instead of pywin32's pythonservice.exe.
        # pythonservice.exe sits in the venv root and links against python3XX.dll,
        # which lives in the *base* interpreter — a venv does not copy it — so it
        # died with 0xC0000135 before reaching any of this code, and the SCM
        # reported it only as "did not respond in a timely fashion". A venv's
        # python.exe resolves its own DLLs and site-packages through pyvenv.cfg.
        # `-m`, never a file path: running the module as a script puts the
        # package directory first on sys.path, where printer_agent's own modules
        # shadow same-named stdlib ones and the import of asyncio fails.
        _exe_name_ = sys.executable
        _exe_args_ = f"-u -m {SERVICE_MODULE}"

        def __init__(self, args):
            super().__init__(args)
            self.stop_requested = threading.Event()
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            self.stop_requested.set()
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self):
            # A service has no console, so the rotating file handler is the only
            # place these records land — and it is what the app's Logs page reads.
            configure_logging()
            # The SCM gives a service 30 seconds to report RUNNING and kills it
            # with error 1053 otherwise. Everything below — reading the config,
            # querying the update feed, dialling the hub — can exceed that on its
            # own, so the handshake has to come first.
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)
            servicemanager.LogInfoMsg("printer-agent service starting")
            try:
                asyncio.run(self._run())
            except ConfigError as exc:
                servicemanager.LogErrorMsg(f"printer-agent configuration error: {exc}")
                raise
            except Exception as exc:  # pragma: no cover - service runtime path
                servicemanager.LogErrorMsg(f"printer-agent service failed: {exc}")
                raise

        async def _run(self):
            config = load_config(CONFIG_PATH)
            if config.updates.auto_update and config.updates.check_on_startup and config.updates.feed_url:
                try:
                    update_status = check_for_update(config.updates.feed_url)
                    if update_status.update_available and update_status.manifest is not None:
                        servicemanager.LogInfoMsg(
                            f"printer-agent update available: {update_status.current_version} -> {update_status.latest_version}"
                        )
                        applied = apply_update(update_status.manifest)
                        servicemanager.LogInfoMsg(applied.message)
                        if applied.installed:
                            servicemanager.LogInfoMsg("printer-agent updated successfully; exiting so the service restarts on the new version")
                            return
                        servicemanager.LogErrorMsg(f"printer-agent update failed: {applied.message}")
                except Exception as exc:  # pragma: no cover - update path is best-effort
                    servicemanager.LogErrorMsg(f"printer-agent update check failed: {exc}")

            outbox = EventOutbox(config.outbox.database_path)
            connection = HubConnection(config, outbox)
            connection_task = asyncio.create_task(connection.run())
            try:
                await asyncio.get_running_loop().run_in_executor(None, self.stop_requested.wait)
            finally:
                connection.stop()
                connection_task.cancel()
                with suppress(asyncio.CancelledError):
                    await connection_task
                outbox.close()
else:
    PrinterAgentService = None


def main() -> int:
    if _IMPORT_ERROR is not None:
        raise SystemExit(
            "Windows service support requires pywin32. Install the project with the windows extra."
        ) from _IMPORT_ERROR

    if len(sys.argv) == 1:
        # No arguments means the SCM launched us as the service process itself
        # (see _exe_args_). Hand over to the control dispatcher; anything else is
        # a person running install/remove/start from a prompt.
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(PrinterAgentService)
        servicemanager.StartServiceCtrlDispatcher()
        return 0

    win32serviceutil.HandleCommandLine(PrinterAgentService)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
