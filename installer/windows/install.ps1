param(
    [string]$InstallRoot = "$env:ProgramFiles\printer-agent",
    [string]$PackageSpec = "",
    [string]$UpdateFeedUrl = "https://github.com/RomanBRempel/printer-agent/releases/latest/download/printer-agent-update.json",
    [bool]$AutoUpdate = $true,
    [switch]$Force,
    [switch]$LaunchGui
)

$ErrorActionPreference = "Stop"

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
        [string]$Description
    )

    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($ShortcutPath)
    $shortcut.TargetPath = $TargetPath
    $shortcut.Arguments = $Arguments
    $shortcut.WorkingDirectory = $WorkingDirectory
    $shortcut.Description = $Description
    $shortcut.IconLocation = "$TargetPath,0"
    $shortcut.Save()
}

if (-not (Test-Administrator)) {
    throw "Run this installer from an elevated PowerShell session."
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$exampleConfig = Join-Path $repoRoot "agent.example.yaml"
$installRootPath = New-Item -ItemType Directory -Force -Path $InstallRoot
$venvPath = Join-Path $installRootPath.FullName ".venv"
$configDir = Join-Path $env:ProgramData "printer-agent"
$configPath = Join-Path $configDir "agent.yaml"
$startMenuDir = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\printer-agent"
$desktopDir = [Environment]::GetFolderPath("Desktop")

$pythonLauncher = Get-PythonLauncher
& $pythonLauncher.Command @pythonLauncher.Args -m venv $venvPath
$pythonExe = Join-Path $venvPath "Scripts\python.exe"
$pythonwExe = Join-Path $venvPath "Scripts\pythonw.exe"

& $pythonExe -m pip install --upgrade pip

if ([string]::IsNullOrWhiteSpace($PackageSpec)) {
    $installTarget = "$($repoRoot)[windows]"
}
else {
    $installTarget = $PackageSpec
}

& $pythonExe -m pip install "$installTarget"

New-Item -ItemType Directory -Force -Path $configDir | Out-Null
if ((-not (Test-Path $configPath)) -or $Force) {
    Copy-Item $exampleConfig $configPath -Force
}

& $pythonExe -c @"
import json
import os
from pathlib import Path

config_path = Path(r'$configPath')
feed_url = r'$UpdateFeedUrl'
auto_update = $AutoUpdate.ToString().ToLower()

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

& $pythonExe -m printer_agent install-service --config $configPath

New-Item -ItemType Directory -Force -Path $startMenuDir | Out-Null
New-Shortcut -ShortcutPath (Join-Path $startMenuDir "printer-agent GUI.lnk") -TargetPath $pythonwExe -Arguments "-m printer_agent gui --config `"$configPath`"" -WorkingDirectory $installRootPath.FullName -Description "Open printer-agent configuration GUI"
New-Shortcut -ShortcutPath (Join-Path $startMenuDir "printer-agent Status.lnk") -TargetPath $pythonExe -Arguments "-m printer_agent --config `"$configPath`" status" -WorkingDirectory $installRootPath.FullName -Description "Show printer-agent status"
New-Shortcut -ShortcutPath (Join-Path $desktopDir "printer-agent GUI.lnk") -TargetPath $pythonwExe -Arguments "-m printer_agent gui --config `"$configPath`"" -WorkingDirectory $installRootPath.FullName -Description "Open printer-agent configuration GUI"

Write-Host "printer-agent was installed to $installRootPath"
Write-Host "Configuration file: $configPath"
Write-Host "Service: printer-agent"
Write-Host "Use the Start Menu shortcut or desktop shortcut to open the GUI."

if ($LaunchGui) {
    Start-Process -FilePath $pythonwExe -ArgumentList @("-m", "printer_agent", "gui", "--config", $configPath) -WorkingDirectory $installRootPath.FullName
}
