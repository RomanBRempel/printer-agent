# Windows installer

This folder contains the Windows install path for `printer-agent`.

## Install

Run `installer/windows/install.cmd` from an elevated prompt or double-click it from Explorer.
The installer:

- creates a local virtual environment under `Program Files\printer-agent`
- installs the package plus Windows service support
- writes the service configuration to `ProgramData\printer-agent\agent.yaml`
- registers the `printer-agent` Windows service
- starts the `printer-agent` Windows service after registration
- creates Start Menu and Desktop shortcuts for the GUI and status view
- leaves update controls in the GUI so operators can check or apply a published package feed

If the window was started by double-click and closes quickly, run from an elevated terminal to inspect output:

```powershell
powershell -ExecutionPolicy Bypass -File installer\windows\install.ps1 -PackageSpec "https://github.com/RomanBRempel/printer-agent/releases/download/v0.1.0a1/printer_agent-0.1.0a1-py3-none-any.whl"
```

## Build a transferable executable installer

Recommended (non-interactive) path:

```powershell
powershell -ExecutionPolicy Bypass -File installer\windows\build-installer-exe-pyinstaller.ps1
```

Alternative path using PowerShell Gallery `ps2exe`:

```powershell
powershell -ExecutionPolicy Bypass -File installer\windows\build-installer-exe.ps1
```

Output files:

- `installer/windows/dist/printer-agent-installer.exe`
- `installer/windows/dist/printer-agent-uninstaller.exe`

Distribution options for another PC:

- Online install: run installer with `-PackageSpec` URL to a wheel.
- Offline install: place `printer_agent-*.whl` next to `printer-agent-installer.exe`; installer will auto-detect it.

## Build a user GUI installer (.exe)

Build a window-based installer for end users:

```powershell
powershell -ExecutionPolicy Bypass -File installer\windows\build-gui-installer-exe.ps1
```

Output file:

- `installer/windows/dist/printer-agent-installer-gui.exe`

What this GUI installer does:

- shows installation progress and log output in a window
- installs and starts the `printer-agent` Windows service
- creates Start Menu/Desktop GUI shortcuts
- asks whether to launch the configuration GUI right after successful install

## Verify result after install

- Service is installed and running:

```powershell
Get-Service printer-agent
```

- Shared config exists:

```powershell
Test-Path "C:\ProgramData\printer-agent\agent.yaml"
```

- GUI shortcut exists:

```powershell
Test-Path "$env:ProgramData\Microsoft\Windows\Start Menu\Programs\printer-agent\printer-agent GUI.lnk"
```

For an alpha release install from GitHub Releases, you can also run:

```powershell
powershell -ExecutionPolicy Bypass -File installer\windows\install.ps1 -PackageSpec "https://github.com/RomanBRempel/printer-agent/releases/download/v0.1.0a1/printer_agent-0.1.0a1-py3-none-any.whl" -UpdateFeedUrl "https://github.com/RomanBRempel/printer-agent/releases/latest/download/printer-agent-update.json" -AutoUpdate $true
```

## Uninstall

Run `installer/windows/uninstall.cmd` from an elevated prompt.
The uninstaller removes the service, shortcuts, and install directory.

Use the `-RemoveConfig` switch with `uninstall.ps1` if you also want to remove the shared configuration under `ProgramData\printer-agent`.
