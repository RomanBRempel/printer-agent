# Windows installer

This folder contains the Windows install path for `printer-agent`.

## Install

Run `installer/windows/install.cmd` from an elevated prompt or double-click it from Explorer.
The installer:

- creates a local virtual environment under `Program Files\printer-agent`
- installs the package plus Windows service support
- writes the service configuration to `ProgramData\printer-agent\agent.yaml`
- registers the `printer-agent` Windows service
- creates Start Menu and Desktop shortcuts for the GUI and status view
- leaves update controls in the GUI so operators can check or apply a published package feed

For an alpha release install from GitHub Releases, you can also run:

```powershell
powershell -ExecutionPolicy Bypass -File installer\windows\install.ps1 -PackageSpec "https://github.com/RomanBRempel/printer-agent/releases/download/v0.1.0a1/printer_agent-0.1.0a1-py3-none-any.whl" -UpdateFeedUrl "https://github.com/RomanBRempel/printer-agent/releases/latest/download/printer-agent-update.json" -AutoUpdate $true
```

## Uninstall

Run `installer/windows/uninstall.cmd` from an elevated prompt.
The uninstaller removes the service, shortcuts, and install directory.

Use the `-RemoveConfig` switch with `uninstall.ps1` if you also want to remove the shared configuration under `ProgramData\printer-agent`.
