from __future__ import annotations

import asyncio
import os
import sys
import threading
from contextlib import suppress
from pathlib import Path

from .aio import run as run_async
from .config import ConfigError, load_config
from .core.outbox import EventOutbox
from .logsetup import configure_logging
from .updater import AutoUpdater
from .uplink.connection import HubConnection

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

#: Seconds the restarter waits before starting the service again. The stop has
#: to finish first — the adapters close their sessions, the outbox closes its
#: database — and a start racing that shutdown fails with "service is stopping".
RESTART_DELAY_S = 8


def restart_command() -> list[str]:
    """The detached command line that stops this service and starts it again.

    A service cannot restart itself: whatever issues the start has to outlive
    the stop, and every thread of this process is gone by then. So the update
    hands the job to a short-lived child.

    Waiting for SCM recovery instead would not work here — recovery actions fire
    on a *crash*, and an update exits cleanly. The alternative, `sc failureflag
    1`, would also restart the service when an operator stops it on purpose,
    which is worse than the problem it solves.
    """
    return [
        "cmd.exe",
        "/c",
        f"sc stop {SERVICE_NAME} & "
        # ping as a sleep: timeout.exe needs a console this process does not have.
        f"ping -n {RESTART_DELAY_S} 127.0.0.1 > nul & "
        f"sc start {SERVICE_NAME}",
    ]


def recovery_command() -> list[str]:
    """Tell the SCM to bring the service back if it dies.

    Deliberately without `failureflag`: recovery then applies to crashes only.
    Turning it on would also restart the service when an operator stops it on
    purpose, which is a worse surprise than the outage it prevents.
    """
    return [
        "sc.exe",
        "failure",
        SERVICE_NAME,
        "reset=",
        "86400",
        "actions=",
        "restart/5000/restart/15000/restart/60000",
    ]


def configure_service_recovery() -> None:
    """Apply the recovery policy. Requires administrator; failure is not fatal."""
    import subprocess

    subprocess.run(recovery_command(), check=False, capture_output=True, text=True)  # noqa: S603


def restart_service() -> None:
    """Ask a detached child to cycle the service, then let this process end."""
    import subprocess

    creation_flags = 0
    if os.name == "nt":  # pragma: no branch - the service only runs on Windows
        creation_flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    subprocess.Popen(  # noqa: S603 - fixed command line, no user input
        restart_command(),
        creationflags=creation_flags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


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
                run_async(self._run())
            except ConfigError as exc:
                servicemanager.LogErrorMsg(f"printer-agent configuration error: {exc}")
                raise
            except Exception as exc:  # pragma: no cover - service runtime path
                servicemanager.LogErrorMsg(f"printer-agent service failed: {exc}")
                raise

        async def _run(self):
            config = load_config(CONFIG_PATH)
            outbox = EventOutbox(config.outbox.database_path)
            connection = HubConnection(config, outbox)
            # The updater runs beside the session rather than before it, so a
            # long-lived service keeps checking; the old code checked once at
            # start, which a box that stays up for weeks never reached again.
            updater = AutoUpdater(
                config,
                is_busy=connection.is_busy,
                restart=self._restart_for_update,
            )
            connection_task = asyncio.create_task(connection.run())
            updater_task = asyncio.create_task(updater.run(), name="printer-agent-updater")
            try:
                await asyncio.get_running_loop().run_in_executor(None, self.stop_requested.wait)
            finally:
                updater.stop()
                connection.stop()
                for task in (updater_task, connection_task):
                    task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await task
                outbox.close()

        def _restart_for_update(self) -> None:
            """Hand the restart to a detached child, then stop this service."""
            servicemanager.LogInfoMsg("printer-agent updated; restarting the service")
            try:
                restart_service()
            except Exception as exc:  # pragma: no cover - service runtime path
                servicemanager.LogErrorMsg(f"printer-agent could not restart itself: {exc}")
                return
            # The child is waiting on our stop; asking for it here means the new
            # version is running seconds later instead of at the next reboot.
            self.stop_requested.set()
            win32event.SetEvent(self.stop_event)
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
