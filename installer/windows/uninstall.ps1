param(
    [string]$InstallRoot = "$env:ProgramFiles\printer-agent",
    [switch]$RemoveConfig
)

$ErrorActionPreference = "Stop"

function Test-Administrator {
    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($currentIdentity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
}

function Try-RemoveShortcut {
    param([string]$ShortcutPath)
    if (Test-Path $ShortcutPath) {
        Remove-Item $ShortcutPath -Force
    }
}

if (-not (Test-Administrator)) {
    throw "Run this uninstaller from an elevated PowerShell session."
}

$installRootPath = Join-Path $InstallRoot ""
$venvPath = Join-Path $installRootPath ".venv"
$configDir = Join-Path $env:ProgramData "printer-agent"
$configPath = Join-Path $configDir "agent.yaml"
$startMenuDir = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\printer-agent"
$desktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "printer-agent GUI.lnk"

if (Test-Path $venvPath) {
    $pythonExe = Join-Path $venvPath "Scripts\python.exe"
    if (Test-Path $pythonExe) {
        try {
            & $pythonExe -m printer_agent uninstall-service | Out-Null
        } catch {
            Write-Warning "Service removal reported an error during uninstall: $($_.Exception.Message)"
        }
    }
}

if (Test-Path $startMenuDir) {
    Remove-Item $startMenuDir -Recurse -Force
}
Try-RemoveShortcut -ShortcutPath $desktopShortcut

if (Test-Path $installRootPath) {
    Remove-Item $installRootPath -Recurse -Force
}

if ($RemoveConfig -and (Test-Path $configDir)) {
    Remove-Item $configDir -Recurse -Force
}

Write-Host "printer-agent was removed."
Write-Host "Configuration was kept in $configPath unless -RemoveConfig was used."
