from __future__ import annotations

import asyncio
import os
import threading
from contextlib import suppress
from pathlib import Path

from .config import ConfigError, load_config
from .core.outbox import EventOutbox
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


if _IMPORT_ERROR is None:
    class PrinterAgentService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        def __init__(self, args):
            super().__init__(args)
            self.stop_requested = threading.Event()
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            self.stop_requested.set()
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self):
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
    win32serviceutil.HandleCommandLine(PrinterAgentService)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
