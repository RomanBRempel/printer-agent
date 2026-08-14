param(
    [string]$OutputDir = (Join-Path $PSScriptRoot "dist")
)

$ErrorActionPreference = "Stop"

$installScript = Join-Path $PSScriptRoot "install.ps1"
$uninstallScript = Join-Path $PSScriptRoot "uninstall.ps1"

if (-not (Test-Path $installScript)) {
    throw "install.ps1 was not found at $installScript"
}
if (-not (Test-Path $uninstallScript)) {
    throw "uninstall.ps1 was not found at $uninstallScript"
}

if (-not (Get-Module -ListAvailable -Name ps2exe)) {
    Write-Host "Installing ps2exe module for current user..."
    Install-Module -Name ps2exe -Scope CurrentUser -Force -AllowClobber
}

Import-Module ps2exe -Force

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$installerExe = Join-Path $OutputDir "printer-agent-installer.exe"
$uninstallerExe = Join-Path $OutputDir "printer-agent-uninstaller.exe"

Invoke-ps2exe -inputFile $installScript -outputFile $installerExe -x64 -title "printer-agent installer" -description "Installs printer-agent service and GUI"
Invoke-ps2exe -inputFile $uninstallScript -outputFile $uninstallerExe -x64 -title "printer-agent uninstaller" -description "Uninstalls printer-agent service and GUI"

Write-Host "Built installer: $installerExe"
Write-Host "Built uninstaller: $uninstallerExe"
Write-Host "If distributing offline, place printer_agent-*.whl next to installer exe or pass -PackageSpec URL/path when running."
