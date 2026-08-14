param(
    [string]$InstallRoot = "$env:ProgramFiles\printer-agent",
    [string]$PackageSpec = "",
    [string]$UpdateFeedUrl = "https://github.com/RomanBRempel/printer-agent/releases/latest/download/printer-agent-update.json",
    [string]$AutoUpdate = "true",
    [switch]$Force,
    [switch]$LaunchGui
)

$ErrorActionPreference = "Stop"

function Invoke-Checked {
    <#
        $ErrorActionPreference = "Stop" does not apply to native commands: a
        non-zero exit is silently ignored. Every step that must succeed goes
        through here, or the installer reports success over a failed install.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$What,
        [Parameter(Mandatory = $true)][scriptblock]$Command
    )

    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed with exit code $LASTEXITCODE."
    }
}

function Test-Administrator {
    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($currentIdentity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
}

function Get-PythonLauncher {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return @{ Command = "py"; Args = @("-3.11") }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return @{ Command = "python"; Args = @() }
    }
    throw "Python 3.11+ was not found. Install Python first or use the py launcher."
}

function New-Shortcut {
    param(
        [string]$ShortcutPath,
        [string]$TargetPath,
        [string]$Arguments,
        [string]$WorkingDirectory,
        [string]$Description,
        [string]$IconLocation = ""
    )

    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($ShortcutPath)
    $shortcut.TargetPath = $TargetPath
    $shortcut.Arguments = $Arguments
    $shortcut.WorkingDirectory = $WorkingDirectory
    $shortcut.Description = $Description
    if ([string]::IsNullOrWhiteSpace($IconLocation)) {
        $shortcut.IconLocation = "$TargetPath,0"
    }
    else {
        $shortcut.IconLocation = "$IconLocation,0"
    }
    $shortcut.Save()
}

function Convert-ToBoolean {
    param([string]$Value)
    $normalized = "$Value".Trim().ToLowerInvariant()
    if ($normalized -in @("1", "true", "yes", "on")) {
        return $true
    }
    if ($normalized -in @("0", "false", "no", "off")) {
        return $false
    }
    throw "Invalid AutoUpdate value: '$Value'. Use true/false."
}

function Resolve-InstallTarget {
    param(
        [string]$ExplicitPackageSpec,
        [string]$ScriptRoot,
        [string]$ManifestUrl
    )

    if (-not [string]::IsNullOrWhiteSpace($ExplicitPackageSpec)) {
        return $ExplicitPackageSpec
    }

    # Bundled wheel first. When run from the installer .exe, $ScriptRoot is the
    # PyInstaller extraction dir, which is where the build script puts it; when
    # run from a checkout, the repo's own dist/ is the next best source. Both
    # rank above the update feed, whose latest release is by definition older
    # than whatever is being installed from here.
    $searchRoots = @($ScriptRoot, (Join-Path $ScriptRoot "..\..\dist"))
    foreach ($root in $searchRoots) {
        $localWheel = Get-ChildItem -Path $root -Filter "printer_agent-*.whl" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
        if ($null -ne $localWheel) {
            return $localWheel.FullName
        }
    }

    if (-not [string]::IsNullOrWhiteSpace($ManifestUrl)) {
        try {
            $manifest = Invoke-RestMethod -Uri $ManifestUrl -Method Get -TimeoutSec 30
            if ($null -ne $manifest -and -not [string]::IsNullOrWhiteSpace($manifest.package_url)) {
                return [string]$manifest.package_url
            }
        }
        catch {
            Write-Warning "Failed to resolve package from update feed '$ManifestUrl': $($_.Exception.Message)"
        }
    }

    throw "PackageSpec is required when no local wheel is present next to install.ps1. Provide -PackageSpec, place printer_agent-*.whl near installer, or configure UpdateFeedUrl with package_url manifest."
}

if (-not (Test-Administrator)) {
    throw "Run this installer from an elevated PowerShell session."
}

Write-Host "[STEP] bootstrap"

$installRootPath = New-Item -ItemType Directory -Force -Path $InstallRoot
$venvPath = Join-Path $installRootPath.FullName ".venv"
$configDir = Join-Path $env:ProgramData "printer-agent"
$configPath = Join-Path $configDir "agent.yaml"
$startMenuDir = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\printer-agent"
$desktopDir = [Environment]::GetFolderPath("Desktop")
$startMenuGuiShortcut = Join-Path $startMenuDir "Printer Agent.lnk"
$desktopGuiShortcut = Join-Path $desktopDir "Printer Agent.lnk"
# Removed in this version: the old console shortcuts opened a terminal window.
$legacyShortcuts = @(
    (Join-Path $startMenuDir "Printer Agent - Configure.lnk"),
    (Join-Path $startMenuDir "Printer Agent - Status.lnk"),
    (Join-Path $desktopDir "Printer Agent - Configure.lnk")
)

$pythonLauncher = Get-PythonLauncher
$pyCmd = $pythonLauncher.Command
$pyArgs = $pythonLauncher.Args
$autoUpdateEnabled = Convert-ToBoolean -Value $AutoUpdate
Invoke-Checked -What "Virtual environment creation" -Command { & $pyCmd @pyArgs -m venv $venvPath }
$pythonExe = Join-Path $venvPath "Scripts\python.exe"
$pythonwExe = Join-Path $venvPath "Scripts\pythonw.exe"
$guiExe = Join-Path $venvPath "Scripts\printer-agent-gui.exe"
# e.g. "311" — pywin32 names its runtime DLLs after the interpreter version.
$pythonVersionTag = (& $pythonExe -c "import sys; print(f'{sys.version_info.major}{sys.version_info.minor}')").Trim()

& $pythonExe -m pip install --upgrade pip

Write-Host "[STEP] package"

$installTarget = Resolve-InstallTarget -ExplicitPackageSpec $PackageSpec -ScriptRoot $PSScriptRoot -ManifestUrl $UpdateFeedUrl

Write-Host "Installing package from: $installTarget"
Invoke-Checked -What "Package install" -Command {
    & $pythonExe -m pip install --upgrade "$installTarget"
}
# Reinstall the package itself unconditionally. A local build carries the same
# version string as the published release, so pip would otherwise decide the old
# code already satisfies the requirement and keep it.
Invoke-Checked -What "Package reinstall" -Command {
    & $pythonExe -m pip install --force-reinstall --no-deps "$installTarget"
}
# Service integration requires pywin32 on Windows even when wheel extras are not requested.
Invoke-Checked -What "pywin32 install" -Command { & $pythonExe -m pip install "pywin32>=306" }
# pip alone is not enough: pythonservice.exe loads pywintypes/pythoncom as plain
# DLLs and cannot see the copies inside site-packages. Without this step the
# service registers fine and then refuses to start, which reads as a mystery.
$postInstall = Join-Path $venvPath "Scripts\pywin32_postinstall.py"
if (Test-Path $postInstall) {
    Invoke-Checked -What "pywin32 post-install" -Command { & $pythonExe $postInstall -install -silent }
}
else {
    throw "pywin32_postinstall.py was not found in $venvPath; the service would register but never start."
}
# Desktop app dependencies. Installed separately from the package spec so a
# plain wheel path, a URL or a PyPI name all work the same way here.
Invoke-Checked -What "PySide6 install" -Command {
    & $pythonExe -m pip install "PySide6-Essentials>=6.7,<7"
}

# Refuse to wire shortcuts to a package that predates the desktop app. Without
# this the install "succeeds" and the operator gets a console window and the old
# Tkinter editor, with nothing pointing at the cause.
& $pythonExe -c "import printer_agent.desktop"
if ($LASTEXITCODE -ne 0) {
    throw "The installed package has no printer_agent.desktop module, so it predates the desktop app. Source was '$installTarget'. Build a current wheel (python -m build --wheel) and pass it with -PackageSpec, or place it next to install.ps1."
}
& $pythonExe -c "import PySide6.QtWidgets"
if ($LASTEXITCODE -ne 0) {
    throw "PySide6 is missing from $venvPath, so the desktop app cannot start."
}

Write-Host "[STEP] config"
New-Item -ItemType Directory -Force -Path $configDir | Out-Null
if ((-not (Test-Path $configPath)) -or $Force) {
    if (Test-Path (Join-Path (Split-Path $PSScriptRoot -Parent | Split-Path -Parent) "agent.example.yaml")) {
        $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
        $exampleConfig = Join-Path $repoRoot "agent.example.yaml"
        Copy-Item $exampleConfig $configPath -Force
    }
    else {
        @"
hub_url: https://rd-control.example.com/api/printers/agent
agent_token: change-me
location_key: location-1
telemetry_interval_s: 5
heartbeat_interval_s: 15
outbox:
    database_path: data/outbox.sqlite3
    max_events: 5000
updates:
    feed_url: ""
    auto_update: false
    check_on_startup: true
printers: []
"@ | Set-Content -Path $configPath -Encoding UTF8
    }
}

& $pythonExe -c @"
import json
import os
from pathlib import Path

config_path = Path(r'$configPath')
feed_url = r'$UpdateFeedUrl'
auto_update = '$autoUpdateEnabled'.lower()

if config_path.exists():
    text = config_path.read_text(encoding='utf-8')
    if text.strip():
        import yaml
        data = yaml.safe_load(text) or {}
    else:
        data = {}
else:
    data = {}

if not isinstance(data, dict):
    data = {}

data.setdefault('updates', {})
data['updates']['feed_url'] = feed_url
data['updates']['auto_update'] = auto_update == 'true'
data['updates']['check_on_startup'] = True

with config_path.open('w', encoding='utf-8') as handle:
    yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True)
"@

Write-Host "[STEP] service"

# The service host needs these next to the system DLLs; pywin32_postinstall put
# them there. Checking here turns a silent non-starting service into a clear
# install failure.
$serviceRuntime = @("pywintypes$($pythonVersionTag).dll", "pythoncom$($pythonVersionTag).dll")
foreach ($dll in $serviceRuntime) {
    if (-not (Test-Path (Join-Path $env:SystemRoot "System32\$dll"))) {
        throw "$dll is not registered in System32, so pythonservice.exe cannot start. The pywin32 post-install step did not take effect."
    }
}

Invoke-Checked -What "Service registration" -Command {
    & $pythonExe -m printer_agent --config $configPath install-service
}

# Confirm the SCM actually knows about it. Registration reporting success while
# the service is absent is exactly the failure the previous installer shipped.
& sc.exe query "printer-agent" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "The printer-agent service is not registered after installation (sc.exe query returned $LASTEXITCODE)."
}

# Starting is best-effort: a fresh install has a template config with no
# printers, so the service cannot run yet. The app's Обзор page shows why and
# offers Start once the configuration is filled in.
& $pythonExe -m printer_agent.windows_service start
if ($LASTEXITCODE -ne 0) {
    Write-Warning "The service is registered but did not start. Configure the hub and printers in the app, then press «Запустить» on the Обзор page."
}

Write-Host "[STEP] shortcuts"
New-Item -ItemType Directory -Force -Path $startMenuDir | Out-Null

if (-not (Test-Path $guiExe)) {
    throw "Desktop app launcher was not found at $guiExe. The installed package is too old for this installer."
}

# The launcher must be a GUI-subsystem executable, or Windows opens a console
# behind the app window. That is what [project.gui-scripts] buys us, and an old
# package built it as a console script instead.
$subsystem = & $pythonExe -c @"
import struct, sys
with open(r'$guiExe', 'rb') as handle:
    data = handle.read(0x400)
offset = struct.unpack_from('<I', data, 0x3C)[0]
print(struct.unpack_from('<H', data, offset + 24 + 68)[0])
"@
if ("$subsystem".Trim() -ne "2") {
    throw "printer-agent-gui.exe is a console launcher (PE subsystem $subsystem), so it would open a terminal. The installed package declares the entry point under [project.scripts] instead of [project.gui-scripts]."
}

# Ship the icon next to the app so shortcuts keep it after an update.
$iconTarget = Join-Path $installRootPath.FullName "printer-agent.ico"
$iconSource = Join-Path $PSScriptRoot "printer-agent.ico"
if (Test-Path $iconSource) {
    Copy-Item $iconSource $iconTarget -Force
}
else {
    $iconTarget = $guiExe
}

foreach ($legacy in $legacyShortcuts) {
    if (Test-Path $legacy) {
        Remove-Item $legacy -Force
    }
}

# Target pythonw.exe rather than the pip-generated printer-agent-gui.exe.
# pythonw.exe is GUI-subsystem, so no console appears, and unlike the entry
# point launcher it is never rewritten by an update — a self-update that
# regenerates a bad launcher must not be able to break the shortcut.
$guiArguments = "-m printer_agent gui --config `"$configPath`""
New-Shortcut -ShortcutPath $startMenuGuiShortcut -TargetPath $pythonwExe -Arguments $guiArguments -WorkingDirectory $installRootPath.FullName -IconLocation $iconTarget -Description "Printer Agent"
New-Shortcut -ShortcutPath $desktopGuiShortcut -TargetPath $pythonwExe -Arguments $guiArguments -WorkingDirectory $installRootPath.FullName -IconLocation $iconTarget -Description "Printer Agent"

Write-Host "printer-agent was installed to $installRootPath"
Write-Host "Configuration file: $configPath"
Write-Host "Service: printer-agent"
Write-Host "Use the Start Menu or desktop shortcut to open Printer Agent."
Write-Host "[STEP] finalize"
& sc.exe query "printer-agent"

if ($LaunchGui) {
    Start-Process -FilePath $pythonwExe -ArgumentList @("-m", "printer_agent", "gui", "--config", $configPath) -WorkingDirectory $installRootPath.FullName
}
