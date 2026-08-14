# Windows installer

This folder contains the Windows install path for `printer-agent`.

## Install

Run `installer/windows/install.cmd` from an elevated prompt or double-click it from Explorer.
The installer:

- creates a local virtual environment under `Program Files\printer-agent`
- installs the package plus Windows service support and the desktop app (PySide6)
- writes the service configuration to `ProgramData\printer-agent\agent.yaml`
- registers the `printer-agent` Windows service
- starts the `printer-agent` Windows service after registration
- creates a single `Printer Agent` shortcut on the Start Menu and the Desktop, pointing at
  `printer-agent-gui.exe` — a GUI-subsystem launcher, so it opens no console window
- removes the older `Printer Agent - Configure` / `Printer Agent - Status` shortcuts, which launched
  through `pythonw`/`python` and opened a terminal
- leaves update controls in the app so operators can check or apply a published package feed

If the window was started by double-click and closes quickly, run from an elevated terminal to inspect output:

```powershell
powershell -ExecutionPolicy Bypass -File installer\windows\install.ps1 -PackageSpec "https://github.com/RomanBRempel/printer-agent/releases/download/v0.1.0a1/printer_agent-0.1.0a1-py3-none-any.whl"
```

## Build the transferable installer (.exe)

```powershell
powershell -ExecutionPolicy Bypass -File installer\windows\build-gui-installer-exe.ps1
```

Output file:

- `installer/windows/dist/printer-agent-installer.exe`

This is the only installer executable. It is built for the Windows GUI subsystem,
so it opens no console at any point, and it is styled with the same Fluent theme
as the desktop app — [gui_installer.py](gui_installer.py) imports
`printer_agent.desktop.theme`, which is why the build passes `--paths src`.

What it does:

- requests elevation, then shows a single window with the install parameters
- reports the named steps of [install.ps1](install.ps1) as a progress timeline
- keeps the PowerShell transcript out of the window: it goes to
  `ProgramData\printer-agent\logs\installer-*.log`, surfaced by a **Журнал**
  button only when a step fails
- offers to open the Printer Agent app when the install succeeds

Distribution options for another PC:

- Online install: pass a wheel URL in the **Пакет** field, or leave it empty to
  take the package from the update feed.
- Offline install: place `printer_agent-*.whl` next to `printer-agent-installer.exe`;
  the installer auto-detects it.

## Build the uninstaller (.exe)

```powershell
powershell -ExecutionPolicy Bypass -File installer\windows\build-uninstaller-exe.ps1
```

Output file:

- `installer/windows/dist/printer-agent-uninstaller.exe`

Removal has no window of its own — this one is a console executable that runs
[uninstall.ps1](uninstall.ps1).

## Verify result after install

- Service is installed and running:

```powershell
Get-Service printer-agent
```

- Shared config exists:

```powershell
Test-Path "C:\ProgramData\printer-agent\agent.yaml"
```

- App shortcut exists:

```powershell
Test-Path "$env:ProgramData\Microsoft\Windows\Start Menu\Programs\printer-agent\Printer Agent.lnk"
```

- The shortcut target opens without a console (GUI subsystem):

```powershell
Test-Path "$env:ProgramFiles\printer-agent\.venv\Scripts\printer-agent-gui.exe"
```

For an alpha release install from GitHub Releases, you can also run:

```powershell
powershell -ExecutionPolicy Bypass -File installer\windows\install.ps1 -PackageSpec "https://github.com/RomanBRempel/printer-agent/releases/download/v0.1.0a1/printer_agent-0.1.0a1-py3-none-any.whl" -UpdateFeedUrl "https://github.com/RomanBRempel/printer-agent/releases/latest/download/printer-agent-update.json" -AutoUpdate $true
```

## Uninstall

Run `installer/windows/uninstall.cmd` from an elevated prompt.
The uninstaller removes the service, shortcuts, and install directory.

Use the `-RemoveConfig` switch with `uninstall.ps1` if you also want to remove the shared configuration under `ProgramData\printer-agent`.
